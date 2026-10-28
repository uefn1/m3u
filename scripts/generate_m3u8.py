import os
import re
import sys
import json
import math
import fractions
import subprocess
import tempfile
import urllib.parse

def parse_release_url(release_url: str):
    pattern = r"github\.com/([^/]+)/([^/]+)/releases/tag/([^/?#]+)"
    match = re.search(pattern, release_url)
    if not match:
        raise ValueError(f"無効な Release URL です: {release_url}")
    
    owner = match.group(1)
    repo = match.group(2)
    tag = urllib.parse.unquote(match.group(3))
    return owner, repo, tag

def clean_float(value_str: str):
    cleaned = value_str.strip().rstrip(",")
    if not cleaned or cleaned == "N/A":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None

def get_duration_from_file(filepath: str) -> float:
    """MP4ファイルから秒数を取得"""
    # 方式1: format / stream duration (MP4はmoovに正確な秒数があるためこれが最も確実)
    cmd_format = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration:stream=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        filepath
    ]
    res_format = subprocess.run(cmd_format, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for v in res_format.stdout.strip().split("\n"):
        val = clean_float(v)
        if val is not None and val > 0:
            return val

    # 方式2: ffmpeg による空デコード (破損・不完全なMP4のフォールバック)
    cmd_ffmpeg = [
        "ffmpeg",
        "-nostats",
        "-i", filepath,
        "-f", "null",
        "-"
    ]
    res_ffmpeg = subprocess.run(cmd_ffmpeg, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time_matches = re.findall(r"time=(\d+):(\d+):(\d+\.\d+)", res_ffmpeg.stderr)
    if time_matches:
        h, m, s = time_matches[-1]
        dur = int(h) * 3600 + int(m) * 60 + float(s)
        if dur > 0:
            return dur

    # 方式3: パケットPTS差分
    cmd_packet = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "packet=pts_time,duration_time",
        "-of", "csv=p=0",
        filepath
    ]
    res_packet = subprocess.run(cmd_packet, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    pts_list = []
    for line in res_packet.stdout.strip().split("\n"):
        parts = line.strip().split(",")
        if parts:
            pts = clean_float(parts[0])
            dur = clean_float(parts[1]) if len(parts) > 1 else 0.0
            if dur is None:
                dur = 0.0
            if pts is not None:
                pts_list.append((pts, dur))

    if pts_list:
        diff = (pts_list[-1][0] + pts_list[-1][1]) - pts_list[0][0]
        if diff > 0:
            return diff

    return None

def main():
    if len(sys.argv) < 2:
        print("Usage: python generate_m3u8.py <RELEASE_URL>")
        sys.exit(1)

    release_url = sys.argv[1].strip()
    owner, repo, tag = parse_release_url(release_url)
    repo_full = f"{owner}/{repo}"
    print(f"ターゲット: {repo_full} (Tag: {tag})")

    # gh CLI で Release のアセット一覧を取得
    gh_cmd = ["gh", "release", "view", tag, "--repo", repo_full, "--json", "assets"]
    res = subprocess.run(gh_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        print(f"gh release view に失敗しました: {res.stderr}")
        sys.exit(1)

    release_data = json.loads(res.stdout)
    assets = release_data.get("assets", [])

    # .mp4 ファイルを抽出
    all_mp4_assets = [a for a in assets if a["name"].lower().endswith(".mp4")]
    if not all_mp4_assets:
        print("対象の .mp4 ファイルが見つかりませんでした。")
        sys.exit(1)

    # fMP4 用の初期化セグメント (init.mp4 など) があれば検知
    init_asset = None
    media_mp4_assets = []
    for a in all_mp4_assets:
        if a["name"].lower() in ["init.mp4", "init.mp4f"]:
            init_asset = a
        else:
            media_mp4_assets.append(a)

    # 自然順ソート (out-000.mp4, out-001.mp4 等)
    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s["name"])]

    media_mp4_assets.sort(key=natural_sort_key)
    total_count = len(media_mp4_assets)
    print(f"合計 {total_count} 個の .mp4 ファイルを 1つずつダウンロード → 秒数解析 → キャッシュ削除 します...\n")

    durations = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        for idx, asset in enumerate(media_mp4_assets, 1):
            name = asset["name"]
            download_url = f"https://github.com/{owner}/{repo}/releases/download/{urllib.parse.quote(tag)}/{name}"
            local_file = os.path.join(tmp_dir, name)

            print(f"[{idx}/{total_count}] ダウンロード中: {name}")

            # 1. 個別ダウンロード
            dl_cmd = [
                "gh", "release", "download", tag,
                "--repo", repo_full,
                "--pattern", name,
                "--dir", tmp_dir,
                "--clobber"
            ]
            dl_res = subprocess.run(dl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if dl_res.returncode != 0:
                print(f"ダウンロード失敗: {name}\n{dl_res.stderr}")
                sys.exit(1)

            # 2. 秒数を取得
            try:
                dur = get_duration_from_file(local_file)
                if dur is None or dur <= 0:
                    print(f"  -> 秒数が特定できなかったため、代替値 10.0 秒を適用します。")
                    dur = 10.0
                else:
                    print(f"  -> 秒数: {dur:.3f} 秒")
                durations.append((name, download_url, dur))
            finally:
                # 3. 直ちにローカルファイルを削除（ディスク解放）
                if os.path.exists(local_file):
                    os.remove(local_file)
                    print(f"  -> キャッシュ削除完了: {name}")

    target_duration = math.ceil(max(d[2] for d in durations))

    # HLS仕様準拠の m3u8 生成 (バージョンは6以上が推奨)
    m3u8_lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        "#EXT-X-PLAYLIST-TYPE:VOD"
    ]

    # init.mp4 が存在した場合は EXT-X-MAP タグを追加
    if init_asset:
        init_name = init_asset["name"]
        init_url = f"https://github.com/{owner}/{repo}/releases/download/{urllib.parse.quote(tag)}/{init_name}"
        m3u8_lines.append(f'#EXT-X-MAP:URI="{init_url}"')

    for name, download_url, dur in durations:
        m3u8_lines.append(f"#EXTINF:{dur:.3f},")
        m3u8_lines.append(download_url)

    m3u8_lines.append("#EXT-X-ENDLIST")

    output_filename = "playlist.m3u8"
    with open(output_filename, "w", encoding="utf-8") as f:
        f.write("\n".join(m3u8_lines) + "\n")

    print(f"\nすべての解析が完了しました。{output_filename} を作成しました。")

if __name__ == "__main__":
    main()
