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
    """
    Release URL から owner, repo, tag を抽出する
    """
    pattern = r"github\.com/([^/]+)/([^/]+)/releases/tag/([^/?#]+)"
    match = re.search(pattern, release_url)
    if not match:
        raise ValueError(f"無効な Release URL です: {release_url}")
    return match.group(1), match.group(2), match.group(3)

def get_release_assets(owner: str, repo: str, tag: str, token: str):
    """GitHub APIからアセット一覧を取得"""
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
    req = urllib.request.Request(api_url)
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    
    with urllib.request.urlopen(req) as res:
        data = json.loads(res.read().decode())
    return data.get("assets", [])

def get_duration(url: str, token: str) -> float:
    """
    ファイルの先頭部分を部分取得し、ffprobe で再生秒数を取得する。
    失敗した場合は全量取得にフォールバック。
    """
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    range_headers = headers.copy()
    range_headers["Range"] = "bytes=0-2097151"

    with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as tmp:
        tmp_path = tmp.name
        req = urllib.request.Request(url, headers=range_headers)
        try:
            with urllib.request.urlopen(req) as response:
                tmp.write(response.read())
        except Exception:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req) as response:
                tmp.write(response.read())

    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        tmp_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    try:
        duration = float(result.stdout.strip())
        return duration
    except ValueError:
        req = urllib.request.Request(url, headers=headers)
        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as tmp:
            tmp_path = tmp.name
            with urllib.request.urlopen(req) as response:
                tmp.write(response.read())
        
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
            
        return float(result.stdout.strip())

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
    print(f"{len(ts_assets)} 個の .ts ファイルが見つかりました。秒数を解析中...")

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

    # ターゲットDuration（最長秒数の切り上げ）
    target_duration = math.ceil(max(d[2] for d in durations))

    # VODシーク対応の m3u8 生成
    m3u8_lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:4",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        "#EXT-X-PLAYLIST-TYPE:VOD"
    ]

    for name, download_url, dur in durations:
        m3u8_lines.append(f"#EXTINF:{dur:.6f},")
        m3u8_lines.append(download_url)

    m3u8_lines.append("#EXT-X-ENDLIST")

    output_filename = "playlist.m3u8"
    with open(output_filename, "w", encoding="utf-8") as f:
        f.write("\n".join(m3u8_lines) + "\n")

    print(f"\n{output_filename} を正常に作成しました。")

if __name__ == "__main__":
    main()
