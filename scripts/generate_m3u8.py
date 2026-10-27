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

def clean_float(value_str: str):
    """カンマや空白を除去して float に変換"""
    cleaned = value_str.strip().rstrip(",")
    if not cleaned or cleaned == "N/A":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None

def get_duration_from_file(filepath: str) -> float:
    """ローカルファイルからPTS差分から正確な秒数を算出"""
    # 映像ストリームからPTSとpacket durationを取得
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,duration_time",
        "-of", "csv=p=0",
        filepath
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    raw_lines = [l.strip() for l in res.stdout.strip().split("\n") if l.strip()]

    # 映像がなければ音声ストリームを試行
    if not raw_lines:
        cmd[4] = "a:0"
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        raw_lines = [l.strip() for l in res.stdout.strip().split("\n") if l.strip()]

    pts_list = []
    for line in raw_lines:
        parts = line.split(",")
        pts = clean_float(parts[0]) if len(parts) > 0 else None
        dur = clean_float(parts[1]) if len(parts) > 1 else 0.0
        if dur is None:
            dur = 0.0
        if pts is not None:
            pts_list.append((pts, dur))

    if pts_list:
        first_pts = pts_list[0][0]
        last_pts = pts_list[-1][0]
        last_dur = pts_list[-1][1]
        
        # 末尾PTS + 最後のパケットの持続時間 - 先頭PTS
        diff = (last_pts + last_dur) - first_pts
        if diff > 0:
            return diff

    # フォールバック: 通常の format/stream duration
    cmd_fallback = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration:stream=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        filepath
    ]
    res = subprocess.run(cmd_fallback, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for v in res.stdout.strip().split("\n"):
        val = clean_float(v)
        if val is not None and val > 0:
            return val

    return None

def get_duration(url: str, token: str) -> float:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as tmp:
        tmp_path = tmp.name

    try:
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

    target_duration = math.ceil(max(d[2] for d in durations))

    m3u8_lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:4",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        "#EXT-X-PLAYLIST-TYPE:VOD"
    ]

    for name, download_url, dur in durations:
        m3u8_lines.append(f"#EXTINF:{dur:.3f},")
        m3u8_lines.append(download_url)

    m3u8_lines.append("#EXT-X-ENDLIST")

    output_filename = "playlist.m3u8"
    with open(output_filename, "w", encoding="utf-8") as f:
        f.write("\n".join(m3u8_lines) + "\n")

    print(f"\n{output_filename} を正常に作成しました。")

if __name__ == "__main__":
    main()
