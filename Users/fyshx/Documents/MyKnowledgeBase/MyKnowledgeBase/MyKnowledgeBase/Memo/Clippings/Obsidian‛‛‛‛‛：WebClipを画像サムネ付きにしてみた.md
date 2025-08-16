---
title: "Obsidian：WebClipを画像サムネ付きにしてみた"
source: "https://wineroses.hatenablog.com/entry/2024/11/15/094052"
author:
  - "[[wineroses (id:wineroses)]]"
published: 2024-11-15
created: 2025-07-13
description: "これはこれでインターネット・データベース。 WebClip Obsidian Web Clipperのクリップを閲覧するdataviewスクリプト。 前回ArcSearch用クリッパーを作ったことで「サムネがつくと視認性が上がる」と気づき改良してみました。 こうなるとReadItLater系のアプリとしてObsidianが使えます。 ```dataviewjs const FOLDER = \"Clippings\" const CSS = \"font-size:medium;\" const p = dv.el(\"input\",\"\") p.placeholder = \"...\" p.style =…"
tags:
  - "clippings"
image: "https://cdn.image.st-hatena.com/image/scale/cfb2e1524b67dd3274d844ad2a055451c3286904/backend=imagemagick;version=1;width=1300/https%3A%2F%2Fgyazo.com%2F3fdcb2708cff40527efdd26f497455d5%2Fraw"
---
## 記事要約

本記事は、ObsidianでWeb Clipperを使ってクリップした記事を、Dataviewスクリプトにより画像サムネイル付きで一覧表示する方法を紹介しています。これにより、Webクリップの視認性を高め、ObsidianをReadItLaterアプリのように活用できることを目的としています。

## 要点

*   **DataviewJSスクリプト**: Webクリップ（指定フォルダ内）をサムネイル付きで一覧表示。
    *   タイトル、著者、descriptionでの検索機能付き。
    *   最終更新日時の降順でソート、表示上限200件。
    *   サムネイル（画像）をタップすると元のWebサイトへ、タイトルをタップするとObsidianノートへ移動。
*   **Obsidian Web Clipper設定**: アプリのテンプレート設定で、プロパティに `image: {{image}}` を追加する必要がある。
*   **効果**: Webクリップの視認性が向上し、アクセスしやすくなる。
*   **課題**: Dataviewの検索はタイトルとdescriptionのみで、本文は対象外。