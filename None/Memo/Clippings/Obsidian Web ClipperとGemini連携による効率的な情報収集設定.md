---
title: "Obsidian Web ClipperとGemini連携による効率的な情報収集設定"
source: "https://qiita.com/vpkaerun/items/2120699db87526174740"
author:
  - "[[vpkaerun]]"
published: 2025-01-12
created: 2025-07-13
description: "Obsidian Web ClipperとGemini連携による効率的な情報収集設定 このドキュメントでは、Obsidian Web Clipper を使用してWebページをクリップし、Google AI (Gemini 1.5 Flash) を活用してMarkdown形..."
tags:
  - "clippings"
image: "https://qiita-user-contents.imgix.net/https%3A%2F%2Fqiita-user-contents.imgix.net%2Fhttps%253A%252F%252Fcdn.qiita.com%252Fassets%252Fpublic%252Farticle-ogp-background-afbab5eb44e0b055cce1258705637a91.png%3Fixlib%3Drb-4.0.0%26w%3D1200%26blend64%3DaHR0cHM6Ly9xaWl0YS11c2VyLXByb2ZpbGUtaW1hZ2VzLmltZ2l4Lm5ldC9odHRwcyUzQSUyRiUyRnFpaXRhLWltYWdlLXN0b3JlLnMzLmFwLW5vcnRoZWFzdC0xLmFtYXpvbmF3cy5jb20lMkYwJTJGMjM1NDc0JTJGcHJvZmlsZS1pbWFnZXMlMkYxNjQ0OTkzNjk1P2l4bGliPXJiLTQuMC4wJmFyPTElM0ExJmZpdD1jcm9wJm1hc2s9ZWxsaXBzZSZiZz1GRkZGRkYmZm09cG5nMzImcz04OTUxODA0NWE1NjZkYjQxYWY5MWRlNTczZjY1YzIzYg%26blend-x%3D120%26blend-y%3D467%26blend-w%3D82%26blend-h%3D82%26blend-mode%3Dnormal%26s%3De67f7eded8d3bc21ffe627fae000a71e?ixlib=rb-4.0.0&w=1200&fm=jpg&mark64=aHR0cHM6Ly9xaWl0YS11c2VyLWNvbnRlbnRzLmltZ2l4Lm5ldC9-dGV4dD9peGxpYj1yYi00LjAuMCZ3PTk2MCZoPTMyNCZ0eHQ9JTIwT2JzaWRpYW4lMjBXZWIlMjBDbGlwcGVyJUUzJTgxJUE4R2VtaW5pJUU5JTgwJUEzJUU2JTkwJUJBJUUzJTgxJUFCJUUzJTgyJTg4JUUzJTgyJThCJUU1JThBJUI5JUU3JThFJTg3JUU3JTlBJTg0JUUzJTgxJUFBJUU2JTgzJTg1JUU1JUEwJUIxJUU1JThGJThFJUU5JTlCJTg2JUU4JUE4JUFEJUU1JUFFJTlBJnR4dC1hbGlnbj1sZWZ0JTJDdG9wJnR4dC1jb2xvcj0lMjMxRTIxMjEmdHh0LWZvbnQ9SGlyYWdpbm8lMjBTYW5zJTIwVzYmdHh0LXNpemU9NTYmdHh0LXBhZD0wJnM9MDhlMzZmMWI1YjJjYWQ3OGE0MTg2MGU4ZTdkZDdkYzM&mark-x=120&mark-y=112&blend64=aHR0cHM6Ly9xaWl0YS11c2VyLWNvbnRlbnRzLmltZ2l4Lm5ldC9-dGV4dD9peGxpYj1yYi00LjAuMCZ3PTgzOCZoPTU4JnR4dD0lNDB2cGthZXJ1biZ0eHQtY29sb3I9JTIzMUUyMTIxJnR4dC1mb250PUhpcmFnaW5vJTIwU2FucyUyMFc2JnR4dC1zaXplPTM2JnR4dC1wYWQ9MCZzPTMxNzUyOTA0MjkxMWM2NTBiNmRhNjBmZGViMWY3MDAw&blend-x=242&blend-y=480&blend-w=838&blend-h=46&blend-fit=crop&blend-crop=left%2Cbottom&blend-mode=normal&s=22f7d7eff6eb0e5971c1ae33bda9b74b"
---
## Obsidian Web ClipperとGemini連携による情報収集設定

この記事は、Obsidian Web ClipperとGoogle Gemini 1.5 Flashを連携させ、Webページを効率的に情報収集・整理する方法について解説しています。

### 設定の主要ステップ

1.  **Chrome拡張機能のインストール**
    *   「Obsidian Web Clipper」をChromeウェブストアからインストール。

2.  **Obsidianの保管庫設定**
    *   拡張機能の設定で、使用するObsidian保管庫名を入力。

3.  **Google APIキーの取得**
    *   Google AI Studioにアクセスし、Gemini APIキーを生成・取得。

4.  **インタプリタの設定**
    *   拡張機能の設定でGoogle Geminiプロバイダーとモデル（`gemini-1.5-flash`）を追加し、APIキーを設定。

5.  **テンプレートの設定**
    *   クリップしたノートの保存場所、ファイル名形式（例: `{{date}}-{{title}}`）、およびGeminiへの指示プロンプトを設定（例: 「ページの内容をメタ認知した上で、Markdown記法の見出し2/見出し3、本文を2レベルの箇条書き、文末にハッシュタグを付与」）。

これらの設定により、Webクリッピング時にGeminiが内容を整形し、Obsidianに整理されたMarkdownノートとして保存されます。