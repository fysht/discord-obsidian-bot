---
title: "Obsidianモバイルの「面倒」をLINEで解決！プラグイン「LINE Notes Sync」導入＆活用ガイド｜松濤Vimmer"
source: "https://note.com/shotovim/n/n55c363144d86"
author:
  - "[[松濤Vimmer]]"
published: 2025-04-02
created: 2025-07-13
description: "作成背景     以前は、iPhoneのObsidianモバイル版を使ってメモを取っていました。しかし、アプリの起動に時間がかかる点や、メモの同期コンフリクトが発生しやすい点に課題を感じていました。特に、移動中や外出先での「閃き」や「気付き」といった瞬間的なアイデアを書き留めたい場合、この手間が心理的なハードルとなり、次第にiPhoneでメモを取る頻度が減ってしまいました。  もっと手軽に、思考を妨げずにアイデアを記録できる方法はないかと考えた結果、日常的に最も頻繁に利用しているLINEに着目しました。LINEであれば、（私にとっては）常に起動しており立ち上げも非常に高速です。また、メ"
tags:
  - "clippings"
image: "https://assets.st-note.com/production/uploads/images/188463611/rectangle_large_type_2_7f682fbe972c0b2b4d860be3c34639aa.png?fit=bounds&quality=85&width=1280"
---
## 記事要約

この記事は、Obsidianモバイル版の起動や同期の課題を解決するため、LINEで手軽にメモを取り、それをObsidianに自動同期する「LINE Notes Sync」プラグインの導入・設定・活用方法を解説しています。

## 要点

### 作成背景
- Obsidianモバイル版の起動遅延、同期コンフリクト解消が目的
- 日常使うLINEで手軽にメモを取りたい
- 課題解決のため自らプラグインを開発

### LINE Notes Sync プラグイン
- LINEでメモした内容をObsidian(Web)に同期
- β版のため不具合の可能性あり

### 導入＆設定手順
1.  **必要なツールのインストール**
    -   **Obsidian側**: コミュニティプラグインから「LINE Notes Sync」を検索・インストール・有効化
    -   **LINE側**: LINE公式アカウント（@078wncqa）を友達追加
2.  **LINE IDの取得**
    -   公式アカウントにメッセージ送信で自動返信される「LINE User ID」を控える
3.  **Obsidian側での詳細設定**
    -   プラグイン設定で以下を入力:
        -   保存先フォルダ
        -   Vault ID (任意のユニークな文字列)
        -   LINE User ID
    -   「Register Mapping」で連携登録

### メッセージ同期
-   **同期方法**: Obsidian左サイドバーのボタンクリック、またはコマンドパレットから「Sync LINE messages」を実行
-   **同期のタイミング**: 手動、または自動同期機能（起動時、定期実行）を利用可能（2025/05/11追加）
-   **同期形式**: 設定フォルダに日時ファイル名でMarkdown形式で保存

### 活用方法
-   メモ整理、アイデアキャプチャ、タスク管理、リマインダーに活用

### トラブルシューティング
-   同期ボタン反応しない、メッセージ同期されない場合はプラグイン有効化、ID設定を確認
-   エラー時は開発者にフィードバック

### リポジトリ情報
-   ソースコードはGitHubで公開: [https://github.com/onikun94/line_to_obsidian](https://github.com/onikun94/line_to_obsidian)
-   フィードバックやバグ報告はIssuesまたは開発者へDM