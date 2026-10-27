import os
import re
import sys
import json
import math
import subprocess
import tempfile
import urllib.request
import urllib.parse

def parse_release_url(release_url: str):
    pattern = r"github\.com/([^/]+)/([^/]+)/releases/tag/([^/?#]+)"
    match = re.search(pattern, release_url)
    if not match:
        raise ValueError(f"無効な Release URL です: {release_url}")
    return match.group(1), match.group(2), match.group(3)

def get_release_assets(owner: str, repo: str, tag: str, token: str):
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
    req = urllib.request.Request(api_url)
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    
    with urllib.request.urlopen(req) as res:
        data = json.loads(res.read().decode())
    return data.get("assets", [])

def get_pts(filepath: str, read_intervals: str) -> float:
    """指定した区間のパケットからPTS（タイムスタンプ）を取得"""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-read_intervals", read_intervals,
        "-select_streams", "v:0",
        "-show_entries", "packet=pts_time",
        "-of", "csv=p=0",
        filepath
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = [line.strip() for line in res.stdout.strip().split("\n") if line.strip() and line.strip() != "N/A"]
    
    # 映像ストリームで取れなかった場合は音声ストリームで再試行
    if not lines:
        cmd[4] = "a:0"
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        lines = [line.strip() for line in res.stdout.strip().split("\n") if line.strip() and line.strip() != "N/A"]

    if not lines:
        return None
    return float(lines[0])

def get_duration_from_file(filepath: str) -> float:
    """ローカルファイルからPTS差分またはコンテナ解析で正確な秒数を算出"""
    # 1. パケット単位で先頭と末尾のタイムスタンプ差分を計算
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "packet=pts_time",
        "-of", "csv=p=0",
        filepath
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = [l.strip() for l in res.stdout.strip().split("\n") if l.strip() and l.strip() != "N/A"]
    
    if not lines:
        # 映像がなければ音声
        cmd[2] = "a:0"
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        lines = [l.strip() for l in res.stdout.strip().split("\n") if l.strip() and l.strip() != "N/A"]

    if lines:
        first_pts = float(lines[0])
        last_pts = float(lines[-1])
        diff = last_pts - first_pts
        if diff > 0:
            return diff

    # 2. 上記で取れなかった場合、通常のformat/stream durationを試行
    cmd_fallback = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration:stream=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        filepath
    ]
    res = subprocess.run(cmd_fallback, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    vals = [v.strip() for v in res.stdout.strip().split("\n") if v.strip() and v.strip() != "N/A"]
    for v in vals:
        try:
            val = float(v)
            if val > 0:
                return val
        except ValueError:
            continue

    return None

def get_duration(url: str, token: str) -> float:
    """
    ファイルをダウンロードし、MPEG-TSのPTS（タイムスタンプ）から正確な秒数を測定する
    """
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        # リダイレクトに対応してダウンロード
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as response, open(tmp_path, "wb") as out_file:
            out_file.write(response.read())

        duration = get_duration_from_file(tmp_path)
        if duration is None or duration <= 0:
            raise ValueError("秒数を特定できませんでした（PTS/Durationが取得不能）")
        return duration
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

def main():
    if len(sys.argv) < 2:
        print("Usage: python generate_m3u8.py <RELEASE_URL>")
        sys.exit(1)

    release_url = sys.argv[1].strip()
    token = os.environ.get("GITHUB_TOKEN", "")

    owner, repo, tag = parse_release_url(release_url)
    print(f"ターゲット: {owner}/{repo} (Tag: {tag})")

    assets = get_release_assets(owner, repo, tag, token)

    ts_assets = [a for a in assets if a["name"].endswith(".ts")]
    if not ts_assets:
        print("対象の .ts ファイルが見つかりませんでした。")
        sys.exit(1)

    # 自然順ソート（out-000.ts, out-001.ts...の順番を維持）
    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s["name"])]

    ts_assets.sort(key=natural_sort_key)
    print(f"{len(ts_assets)} 個の .ts ファイルを解析します...")

    durations = []
    for asset in ts_assets:
        name = asset["name"]
        download_url = asset["browser_download_url"]
        
        try:
            dur = get_duration(download_url, token)
            print(f"- {name}: {dur:.3f} 秒")
            durations.append((name, download_url, dur))
        except Exception as e:
            print(f"Error fetching duration for {name}: {e}")
            sys.exit(1)

    # 最大秒数の切り上げ（HLS仕様準拠）
    target_duration = math.ceil(max(d[2] for d in durations))

    m3u8_lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:4",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        "#EXT-X-PLAYLIST-TYPE:VOD"
    ]

    for name, download_url, dur in durations:
        # 小数点以下3桁〜6桁で正確に記載
        m3u8_lines.append(f"#EXTINF:{dur:.3f},")
        m3u8_lines.append(download_url)

    m3u8_lines.append("#EXT-X-ENDLIST")

    output_filename = "playlist.m3u8"
    with open(output_filename, "w", encoding="utf-8") as f:
        f.write("\n".join(m3u8_lines) + "\n")

    print(f"\n{output_filename} を正常に作成しました。")

if __name__ == "__main__":
    main()
