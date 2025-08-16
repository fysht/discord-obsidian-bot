---
title: "【Obsidian × Whisper】アイデアを逃さずにメモする方法"
source: "https://zenn.dev/ryosuke_kawata/articles/6d36289552039e"
author:
  - "[[Zenn]]"
published: 2025-05-31
created: 2025-07-13
description:
tags:
  - "clippings"
image: "https://res.cloudinary.com/zenn/image/upload/s--4g766_wP--/c_fit%2Cg_north_west%2Cl_text:notosansjp-medium.otf_55:%25E3%2580%2590Obsidian%2520%25C3%2597%2520Whisper%25E3%2580%2591%25E3%2582%25A2%25E3%2582%25A4%25E3%2583%2587%25E3%2582%25A2%25E3%2582%2592%25E9%2580%2583%25E3%2581%2595%25E3%2581%259A%25E3%2581%25AB%25E3%2583%25A1%25E3%2583%25A2%25E3%2581%2599%25E3%2582%258B%25E6%2596%25B9%25E6%25B3%2595%2Cw_1010%2Cx_90%2Cy_100/g_south_west%2Cl_text:notosansjp-medium.otf_37:Ryosuke%2520Kawata%2Cx_203%2Cy_121/g_south_west%2Ch_90%2Cl_fetch:aHR0cHM6Ly9zdG9yYWdlLmdvb2dsZWFwaXMuY29tL3plbm4tdXNlci11cGxvYWQvYXZhdGFyLzkzZTNhNzM5YjkuanBlZw==%2Cr_max%2Cw_90%2Cx_87%2Cy_95/v1627283836/default/og-base-w1200-v2.png"
---
# 記事要約

本記事は、Obsidian、Whisper、Geminiを連携させ、キーボードショートカットによる音声入力で手軽にアイデアをメモし、自動的にObsidianのデイリーノートに追記する仕組みについて解説しています。

## 要点

### 背景

*   Obsidian (特にThinoプラグイン) を知的生産ツールとして活用。
*   アイデアを逃さず記録するため、より手軽なメモ方法を模索。
*   別の作業中でもサクッとメモしたいというニーズ。

### 解決策

*   PCのキーボードショートカット（例: Shift + Command + A長押し）で録音を開始・停止。
*   録音停止後、自動的に以下の処理を実行：
    1.  **Whisper**: 音声ファイルを文字起こし。
    2.  **Gemini**: 文字起こしテキストの誤字脱字、フィラーワードなどを校正・修正。
    3.  **Obsidian**: 校正済みテキストをデイリーノートに追記。

### 実装に必要なツールと手順

1.  **Obsidian**: インストールし、Thinoプラグインを導入・有効化。
2.  **Whisper**: `whisper.cpp`をクローンし、`small`モデルでビルド。
3.  **Hammerspoon**: Homebrewでインストール。
    *   macOSのオートメーションツールとしてキーボード操作をフック。
    *   システム環境設定でアクセシビリティ権限を付与する必要あり。
    *   設定ファイル`init.lua`にSoX, Whisper, Gemini連携のLuaスクリプトを記述。
4.  **SoX (Sound eXchange)**: Homebrewでインストール。
    *   コマンドラインで音声録音に使用。
5.  **Gemini API Key**: Google AI Studioで取得し、Hammerspoonスクリプトに設定。

### カスタマイズ例

*   ホットキーの変更。
*   Whisperモデルのサイズ変更（速度優先など）。
*   Geminiの校正プロンプト変更。
*   追記先ノートの変更（週別、プロジェクト別など）。

### まとめ

*   キーボードワンタッチで録音・文字起こし・校正・Obsidian追記が自動化される。
*   アイデアを即座に記録し、日々のノートフローに組み込みやすくなる。
*   紹介されたスクリプトを基に、個々のワークフローに合わせて拡張可能。