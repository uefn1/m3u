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
    
    owner = match.group(1)
    repo = match.group(2)
    # URLエンコードされている可能性があるためデコード（%2B -> + など）
    raw_tag = match.group(3)
    tag = urllib.parse.unquote(raw_tag)
    return owner, repo, tag

def get_release_assets(owner: str, repo: str, tag: str, token: str):
    # GitHub API のパス用に再度適切にクォート
    quoted_tag = urllib.parse.quote(tag, safe='')
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{quoted_tag}"
    req = urllib.request.Request(api_url)
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    
    with urllib.request.urlopen(req) as res:
        data = json.loads(res.read().decode())
    return data.get("assets", [])

def clean_float(value_str: str):
    cleaned = value_str.strip().rstrip(",")
    if not cleaned or cleaned == "N/A":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None

def get_duration_from_file(filepath: str) -> float:
    # 方式1: ffmpeg で null 出力に流し込み、実際にデコードされた最後のタイムスタンプを取得（最も確実・壊れたTSにも対応）
    cmd_ffmpeg = [
        "ffmpeg",
        "-nostats",
        "-i", filepath,
        "-f", "null",
        "-"
    ]
    res_ffmpeg = subprocess.run(cmd_ffmpeg, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # stderr の最終行付近から time=00:00:10.05 のような表記を正規表現で探す
    time_matches = re.findall(r"time=(\d+):(\d+):(\d+\.\d+)", res_ffmpeg.stderr)
    if time_matches:
        h, m, s = time_matches[-1]
        calc_dur = int(h) * 3600 + int(m) * 60 + float(s)
        if calc_dur > 0:
            return calc_dur

    # 方式2: パケットの PTS から先頭と末尾の差分を計算（ストリームを限定せず全体で検索）
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
        first_pts = pts_list[0][0]
        last_pts = pts_list[-1][0]
        last_dur = pts_list[-1][1]
        diff = (last_pts + last_dur) - first_pts
        if diff > 0:
            return diff

    # 方式3: 通常の format/stream duration
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

    return None

def get_duration(url: str, token: str) -> float:
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as response, open(tmp_path, "wb") as out_file:
            out_file.write(response.read())

        # ダウンロードしたファイルが小さすぎる（HTMLエラーページなどの場合）を検知
        file_size = os.path.getsize(tmp_path)
        if file_size < 1024:
            with open(tmp_path, "r", errors="ignore") as f:
                content = f.read()
            raise ValueError(f"ファイルサイズが小さすぎます ({file_size} bytes): {content[:100]}")

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
