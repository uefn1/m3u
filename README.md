# m3u-gen

Release URLを入力してVLCで通常バー表示されるVOD用.m3u8を生成するGoツール

## 特徴
- HTML不要、Goのみ
- 先頭128KBだけRange取得してmoov/mvhdをパース → 20並列で10件0.5秒
- VLCでライブ判定されない: PLAYLIST-TYPE:VOD + INDEPENDENT-SEGMENTS + ENDLIST
- 正確なEXTINFで総時間表示・シーク可能

## 使い方

### 1. 単一URLから生成
```bash
go run . -url https://github.com/uefn1/go-stream-capturer/releases/tag/rec-FIFA%2B-20261026-2137 -o playlist.m3u8
vlc playlist.m3u8
```

### 2. ymlから複数生成
config.ymlを編集:
```yml
releases:
  - url: https://github.com/uefn1/go-stream-capturer/releases/tag/rec-FIFA%2B-20261026-2137
    output: playlist-FIFA.m3u8
```

```bash
go run . -config config.yml
```

### 3. GitHub Actionsで自動生成
- Actions > Generate m3u8 from Release > Run workflow > release_urlを入力
- またはRelease作成時に自動で.m3u8を生成してReleaseアセットに添付

## yml仕様
```yml
releases:
  - url: "https://github.com/.../releases/tag/..."
    output: "playlist.m3u8"
settings:
  segdur: 600.0
  target: 610.0
  concurrency: 20
  prefer_mp4: true
  no_probe: false
```
