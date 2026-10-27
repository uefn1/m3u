# --- 修正前 ---
    # m3u8_lines = [
    #     "#EXTM3U",
    #     "#EXT-X-VERSION:3",
    #     f"#EXT-X-TARGETDURATION:{target_duration}",
    #     "#EXT-X-MEDIA-SEQUENCE:0"
    # ]

    # --- 修正後 ---
    m3u8_lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:4",               # 小数点付きEXTINFに対応するためバージョン4以上を推奨
        f"#EXT-X-TARGETDURATION:{target_duration}",
        "#EXT-X-PLAYLIST-TYPE:VOD",       # ★これを入れることでオンデマンド(シーク可能)として認識される
    ]

    for name, download_url, dur in durations:
        m3u8_lines.append(f"#EXTINF:{dur:.6f},")
        m3u8_lines.append(download_url)

    m3u8_lines.append("#EXT-X-ENDLIST")   # ★この終端タグとVODタグが合わさることで完全な録画ファイルと判断される
