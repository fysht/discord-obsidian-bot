---
title: "Obsidian Web Clipper × AI でWebページを自動要約＆ストックしてみた | DevelopersIO"
source: "https://dev.classmethod.jp/articles/try-obsidian-web-clipper-ai-summary/"
author:
  - "[[toyoshima-masaya]]"
published: 2025-07-18
created: 2025-07-13
description:
tags:
  - "clippings"
image: "https://images.ctfassets.net/ct0aopd36mqt/271wVAxeN1rlKRsu2HX3i2/47104a220a7c282e824443292b7ac5bb/eyecatch_obsidian_1200x630.jpg"
---
## Webページの自動要約＆ストック方法 (Obsidian Web Clipper × AI)

この記事では、Obsidianのブラウザ拡張機能「Obsidian Web Clipper」とAIを活用してWebページを自動で要約し、Obsidianに保存する方法を紹介しています。Obsidianユーザーや、後で読みたい記事が溜まっている人におすすめの活用法です。

### 設定方法

*   **インタープリターの設定**
    *   Obsidian Web Clipperの設定画面を開く。
    *   サイドバーから「インタープリター」を選択。
    *   プロバイダー（Google Gemini, DeepSeek, OpenAIなど）を指定し、APIキーを入力。
    *   使用するモデルを選択（例: Gemini 2.0 Flash）。
    *   詳細設定は`{{fullHtml}}`にする。

*   **テンプレートの設定**
    *   サイドバーから「テンプレート」を選択。
    *   ノートの保存先ディレクトリを指定。
    *   「ノートの内容」にAIへのプロンプトを記載（例: 「内容を簡潔に要約してください...」）。

### Webページの要約実行

*   要約したいWebページを開く。
    *   ブラウザのObsidian Web Clipper拡張機能を開くと、自動で要約が開始される。
    *   要約された内容はMarkdown形式でObsidianに保存される。
    *   保存されたノートには、元の記事へのリンクも含まれるため、いつでも本文を確認できる。

### まとめ

この方法を利用することで、気になる記事を手軽に要約してObsidianにストックし、後から効率的に見返すことが可能になります。