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

def parse_fps(fps_str: str) -> float:
    """r_frame_rate や avg_frame_rate (例: '30/1' や '30000/1001') を float に変換"""
    if not fps_str or fps_str == "N/A" or fps_str == "0/0":
        return None
    try:
        return float(fractions.Fraction(fps_str))
    except Exception:
        return None

def get_duration_from_frames(filepath: str) -> float:
    """
    PTS/Duration が無い場合の強力なフォールバック:
    フレーム数（または音声サンプル数）を直接カウントして秒数を計算する
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-count_frames",
        "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_frames,r_frame_rate,avg_frame_rate",
        "-of", "json",
        filepath
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        data = json.loads(res.stdout)
        streams = data.get("streams", [])
        if streams:
            stream = streams[0]
            frames = int(stream.get("nb_read_frames", 0))
            fps = parse_fps(stream.get("avg_frame_rate")) or parse_fps(stream.get("r_frame_rate"))
            if frames > 0 and fps and fps > 0:
                calc_dur = frames / fps
                return calc_dur
    except Exception:
        pass

    # 映像で取れなければ音声のサンプル数から計算
    cmd_audio = [
        "ffprobe",
        "-v", "error",
        "-count_packets",
        "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate,duration_ts,nb_read_packets",
        "-show_entries", "format=duration",
        "-of", "json",
        filepath
    ]
    res_audio = subprocess.run(cmd_audio, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        data = json.loads(res_audio.stdout)
        streams = data.get("streams", [])
        if streams:
            stream = streams[0]
            sr = int(stream.get("sample_rate", 0))
            # duration_ts (タイムスタンプティック数) があれば
            dur_ts = clean_float(str(stream.get("duration_ts", "")))
            if dur_ts and sr > 0:
                return dur_ts / sr
    except Exception:
        pass

    # 最終手段: ビットレートとファイルサイズから概算
    cmd_bitrate = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=bit_rate",
        "-of", "default=noprint_wrappers=1:nokey=1",
        filepath
    ]
    res_bitrate = subprocess.run(cmd_bitrate, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    bitrate = clean_float(res_bitrate.stdout)
    if bitrate and bitrate > 0:
        file_size_bytes = os.path.getsize(filepath)
        calc_dur = (file_size_bytes * 8) / bitrate
        if calc_dur > 0:
            return calc_dur

    return None

def get_duration_from_file(filepath: str) -> float:
    # 方式1: ffmpeg で最後まで空デコードして進捗タイムスタンプを取得
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

    # 方式2: パケットの PTS (最初と最後) の差分
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

    # 方式3: format/stream duration
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

    # 方式4: 【新設】PTS/Duration が一切存在しない場合のフォールバック（フレーム数やサンプル数から算出）
    fallback_dur = get_duration_from_frames(filepath)
    if fallback_dur and fallback_dur > 0:
        return fallback_dur

    return None

def main():
    if len(sys.argv) < 2:
        print("Usage: python generate_m3u8.py <RELEASE_URL>")
        sys.exit(1)

    release_url = sys.argv[1].strip()
    owner, repo, tag = parse_release_url(release_url)
    repo_full = f"{owner}/{repo}"
    print(f"ターゲット: {repo_full} (Tag: {tag})")

    # gh CLI を使って Release のアセット一覧を取得
    gh_cmd = ["gh", "release", "view", tag, "--repo", repo_full, "--json", "assets"]
    res = subprocess.run(gh_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        print(f"gh release view に失敗しました: {res.stderr}")
        sys.exit(1)

    release_data = json.loads(res.stdout)
    assets = release_data.get("assets", [])

    ts_assets = [a for a in assets if a["name"].endswith(".ts")]
    if not ts_assets:
        print("対象の .ts ファイルが見つかりませんでした。")
        sys.exit(1)

    # 自然順ソート
    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s["name"])]

    ts_assets.sort(key=natural_sort_key)
    print(f"{len(ts_assets)} 個の .ts ファイルをダウンロードして解析します...")

    durations = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        for asset in ts_assets:
            name = asset["name"]
            # パブリックリリースの直接ダウンロードURL
            download_url = f"https://github.com/{owner}/{repo}/releases/download/{urllib.parse.quote(tag)}/{name}"
            
            # gh release download で確実にダウンロード
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

            local_file = os.path.join(tmp_dir, name)
            dur = get_duration_from_file(local_file)
            
            # 万が一どの手段でも取れなかった場合のセーフガード（デフォルト10秒とするなど）
            if dur is None or dur <= 0:
                print(f"Warning: {name} の秒数が特定できなかったため、代替値 10.0 秒を適用します。")
                dur = 10.0

            print(f"- {name}: {dur:.3f} 秒")
            durations.append((name, download_url, dur))

            if os.path.exists(local_file):
                os.remove(local_file)

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
