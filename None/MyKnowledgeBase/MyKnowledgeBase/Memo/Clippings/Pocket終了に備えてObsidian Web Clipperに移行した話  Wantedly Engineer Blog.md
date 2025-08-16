---
title: "Pocket終了に備えてObsidian Web Clipperに移行した話 | Wantedly Engineer Blog"
source: "https://www.wantedly.com/companies/wantedly/post_articles/978503"
author:
  - "[[冨永 康二郎]]"
  - "[[Wantedly]]"
  - "[[Inc.]]"
published: 2025-05-28
created: 2025-07-13
description: "こんにちは。ウォンテッドリーのEnablingチームでバックエンドエンジニアをしている冨永(@kou_tominaga)です。Enablingチームでは技術的な取り組みを社外にも発信すべく、メン..."
tags:
  - "clippings"
image: "https://images.wantedly.com/i/33AHpv2?w=1200&h=630&style=cover"
---
# Pocket終了に備えてObsidian Web Clipperに移行した話

Mozilla Pocketのサービス終了（2025年7月8日）に伴い、代替ツールとしてObsidian Web Clipperに移行した経緯と方法について解説。

## Obsidian Web Clipperを選んだ理由

- PCとモバイルの両方から「あとで読む」の登録が可能
- 豊富な拡張機能による高いカスタマイズ性
- Markdown形式での保存（LLM解析や活用に適している）
- 既存のObsidian環境で情報の一元管理が可能

## Obsidian Web Clipperの導入手順

- Webクリップ専用のVaultまたはフォルダを準備
- 利用ブラウザにObsidian Web Clipper拡張機能をインストール
- 拡張機能でVaultパス、保存フォルダパス、テンプレートを設定
- テンプレートでタイトル、URL、作成日、タグなどのプロパティを定義

## Pocketからのデータ移行

- Pocketから保存済み記事をCSV形式でエクスポート
- Rubyスクリプトを使用してCSVデータをMarkdownファイルに変換

## Obsidianでの設定

- コミュニティプラグイン「Dataview」をインストールし、JavaScriptクエリを有効化
- テーマ「Minimal」を有効化（カード表示のため）
- DataviewJSクエリを記述したファイルを作成し、移行した記事をカード形式で表示

## まとめ

Pocket終了を機に情報管理方法を見直し、情報収集を「読む」から「活用する」スタンスへ。Obsidian Web Clipperは情報資産化に適したツールである。
