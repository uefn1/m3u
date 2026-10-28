import os
import re
import sys
import json
import math
import subprocess
import tempfile
import urllib.parse

def parse_release_url(release_url: str):
    """Release URLから owner, repo, tag を抽出"""
    pattern = r"github\.com/([^/]+)/([^/]+)/releases/tag/([^/?#]+)"
    match = re.search(pattern, release_url)
    if not match:
        raise ValueError(f"無効な Release URL です: {release_url}")
    
    owner = match.group(1)
    repo = match.group(2)
    tag = urllib.parse.unquote(match.group(3))
    return owner, repo, tag

def get_mp4_duration(filepath: str) -> float:
    """
    独立した完全な MP4 ファイルからメタデータ (moov) の再生秒数を取得
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        filepath
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    val = res.stdout.strip().rstrip(",")
    if val and val != "N/A":
        try:
            dur = float(val)
            if dur > 0:
                return dur
        except ValueError:
            pass

    cmd_stream = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        filepath
    ]
    res_stream = subprocess.run(cmd_stream, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    val_stream = res_stream.stdout.strip().rstrip(",")
    if val_stream and val_stream != "N/A":
        try:
            dur = float(val_stream)
            if dur > 0:
                return dur
        except ValueError:
            pass

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

    # .mp4 ファイルのみを抽出
    mp4_assets = [a for a in assets if a["name"].lower().endswith(".mp4")]
    if not mp4_assets:
        print("対象の .mp4 ファイルが見つかりませんでした。")
        sys.exit(1)

    # 自然順ソート（out-000.mp4, out-001.mp4 ...）
    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s["name"])]

    mp4_assets.sort(key=natural_sort_key)
    total_count = len(mp4_assets)
    print(f"合計 {total_count} 個の単体再生可能 .mp4 ファイルを順次解析します...\n")

    durations = []

    # 1ファイルずつ「ダウンロード → 秒数取得 → 即時キャッシュ削除」
    with tempfile.TemporaryDirectory() as tmp_dir:
        for idx, asset in enumerate(mp4_assets, 1):
            name = asset["name"]
            download_url = f"https://github.com/{owner}/{repo}/releases/download/{urllib.parse.quote(tag)}/{name}"
            local_file = os.path.join(tmp_dir, name)

            print(f"[{idx}/{total_count}] ダウンロード中: {name}")

            # 該当ファイルのみダウンロード
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

            # 秒数を取得
            try:
                dur = get_mp4_duration(local_file)
                if dur is None or dur <= 0:
                    print(f"  -> 秒数が特定できなかったため、代替値 10.0 秒を適用します。")
                    dur = 10.0
                else:
                    print(f"  -> 再生時間: {dur:.3f} 秒")
                durations.append((name, download_url, dur))
            finally:
                # 解析完了後、即座にローカルファイルを完全削除（ディスク容量を解放）
                if os.path.exists(local_file):
                    os.remove(local_file)
                    print(f"  -> キャッシュ削除完了: {name}")

    target_duration = math.ceil(max(d[2] for d in durations))

    # 独立した MP4 セグメント用の HLS プレイリスト構築
    m3u8_lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        "#EXT-X-PLAYLIST-TYPE:VOD"
    ]

    for idx, (name, download_url, dur) in enumerate(durations):
        # 独立したMP4同士の境界にはヘッダー再初期化用の DISCONTINUITY を挿入
        if idx > 0:
            m3u8_lines.append("#EXT-X-DISCONTINUITY")
        m3u8_lines.append(f"#EXTINF:{dur:.3f},")
        m3u8_lines.append(download_url)

    m3u8_lines.append("#EXT-X-ENDLIST")

    output_filename = "playlist.m3u8"
    with open(output_filename, "w", encoding="utf-8") as f:
        f.write("\n".join(m3u8_lines) + "\n")

    pri
