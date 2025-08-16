---
title: "Cursorよりおすすめ？『Gemini Code Assist』の使い方｜わたヤク"
source: "https://note.com/ai_biostat/n/n46574c065fdb"
author:
  - "[[わたヤク]]"
published: 2025-07-07
created: 2025-07-16
description: "優秀なAIコーディングアシスタントが無料で使えたら嬉しいですよね？ 今回ご紹介する「Gemini Code Assist」はそんなニーズを満たしてくれる優秀なVS Codeの拡張機能です。  ↓ 公式ページ  Gemini Code Assist | AI coding assistantGet AI coding and programming help no matter the language orcodeassist.google  個人向けの Gemini Code Assist を使用したコード  |  Google"
tags:
  - "clippings"
---
![見出し画像](https://assets.st-note.com/production/uploads/images/200778371/rectangle_large_type_2_733f5b1de3d473270e5eca11e7d566a5.png?width=1200)

## Cursorよりおすすめ？『Gemini Code Assist』の使い方

[わたヤク](https://note.com/ai_biostat)

優秀なAIコーディングアシスタントが無料で使えたら嬉しいですよね？  
今回ご紹介する「Gemini Code Assist」はそんなニーズを満たしてくれる優秀なVS Codeの拡張機能です。

↓ 公式ページ

[**Gemini Code Assist | AI coding assistant** *Get AI coding and programming help no matter the language or* *codeassist.google*](https://codeassist.google/)

[**個人向けの Gemini Code Assist を使用したコード | Google for Developers** *IDE で Gemini Code Assist を使用する方法。* *developers.google.com*](https://developers.google.com/gemini-code-assist/docs/write-code-gemini?hl=ja)

AIコーディングといえばCursorが有名です。  
私もCursorはとても気に入っており（ほぼ）専用のブログも書いています。

[**AI 医療統計Introduction - AI 医療統計** *はじめに このウェブサイトでは、「誰もが自分のやりたい統計解析を実現できる」ことを目標に掲げ、最新のAIを活用した医療統計* *ai-biostat.com*](https://ai-biostat.com/)

Cursorをずっと使っている愛着もあるのですが、データ分析の分野でAIコーディングするときはCursorが一番使いやすいとずっと思っていました。

- 行単位で修正部分を指定できる
- 修正箇所はエディタ上で差分がわかりやすく表示される
- 無料版でも優秀なAIが使えるので誰にでもおすすめできる

Gemini Code Assistを使ってみてまず驚いたのは、これらのCursorの長所がほぼカバーされていたことです。さらに、Googleお得意の太っ腹戦略により

- 無料でGemini 2.5モデルが使える
- 240回/日のチャットリクエスト枠
- 6000回/日のコード補完

など、完全にCursorを喰いにきています。

「これは知っておかないとあかんな」という感じでnoteを書くに至りました。

## Gemini Code Assistの導入方法

Gemini Code Assistの導入方法を紹介します。めちゃくちゃ簡単です。

① VS Codeの拡張機能「Gemini Code Assist」をインストール

![画像](https://assets.st-note.com/img/1751886139-1mkYiVBle9fpdN8JcAaQOWPz.png?width=1200)

② 「Sign in」から使用するGoogleアカウントを選択

![画像](https://assets.st-note.com/img/1751886868-xUu7QbnDWdg3AHijkElR5NYo.png?width=1200)

これだけで準備完了です。

## Gemini Code Assistの使い方

### プロンプトの入力

Gemini Code Assistを開くと画像のようにプロンプト入力欄が表示されます。ほぼCursorと同じように使えます。

![画像](https://assets.st-note.com/img/1751887165-yjNcZ0X8m1IehCOuz2SlW5dY.png?width=1200)

### @参照機能

@\`ファイル名\`とすることでファイルを参照させることができます。参照されたファイルをもとにAIが回答してくれます。

![画像](https://assets.st-note.com/img/1751887991-XBUr4lZfic6pgIzSu2VQPs5R.png)

\`@\`を入力すると、自動で作業ディレクトリの中から候補となるファイルが提示されます。今回は「データ形式.md」というファイルを参照してみました。  
「Context items」の欄にデータ形式.mdが加えられているのが分かります。  
また、先頭で開いているファイルはデフォルトで参照される仕様になっています。この辺りもCursorと同じですね。

### 提案されたコードの挿入

プロンプトに対してAIがコードを提案してくれます。  
新たにコードを追加するときは画像の「＋」ボタンを押すと編集中のファイルにコードが追加されます。

![画像](https://assets.st-note.com/img/1751888256-Tjb5PsCWrFmfD4Uw2vaeViJG.png?width=1200)

### コードの修正：修正箇所を細かく指定できる

Cursorで気に入っていたポイントの「修正箇所を細かく指定できる」ところもGemini Code Assistでできました。  
修正したいコードをドラッグで選択して、画像左の「Add to Chat Context」を押すだけです。  
Claude CodeやGemini CLIではなかなか難しかった細かい修正が簡単にできるのも大きなメリットです。

![画像](https://assets.st-note.com/img/1751889483-XYjQW4kyuzahbtMoFpBmqwRV.png?width=1200)

### コードの修正：差分表示がおすすめ

もちろん既存のコードを修正することもできます。  
修正があったときは、画像で赤丸を付けた差分表示ボタンを押しましょう。

![画像](https://assets.st-note.com/img/1751888366-HTPZoc5m2u3MYGwkDBLxnbEr.png?width=1200)

修正前（赤）と修正後（黄緑）で横並びに差分を比較することもできます。  
右上の承認ボタンを押すと変更箇所全体（緑色）が反映されます。

![画像](https://assets.st-note.com/img/1751888455-o7LUsMXurKTheg1CbvPtOVZj.png?width=1200)

下画像の赤丸で囲った「→」を押すと変更箇所ごとに修正前の状態に戻すことができます。

![画像](https://assets.st-note.com/img/1751894142-6wcjGi1OsSDkxvfuyT0Vp3CE.png?width=1200)

エディタ上でこの差分表示できるのが個人的に一番嬉しかったポイントです。

### コード補完機能

6000回/日とほぼ無制限でコード補完機能が使えます。  
性能はCursorと比べると2段階くらい劣っています。  
Cursorの無料プランではコード補完機能が実質的に機能していないので、Gemini Code Assistのコード補完機能はCursorの無料プラン以上、有料プラン以下という感じです。

### QuickなAIの呼び出し

エディタ上で\`Ctrl (⌘) + I\`を押すと、下の画像のようにエディタ上部にプロンプト入力欄が表示され、複数のコマンドから用途に合わせた指示を送ることができます。

![画像](https://assets.st-note.com/img/1751895310-5wekmfL89ohYKDysO7T1VIWi.png?width=1200)

> /generate：自然言語で指示した内容についてコードを生成  
> /fix ：エラーを修正  
> Explain this ：選択したコードについて説明  
> Generate unit tests ：動作検証のためのコードを生成

### AIモデルの性能

Gemini Code AssistではGeminiの2.5モデルが動いているようです。  
使ってみた感じ、Cursorの無料プランで使えるGemini 2.5 Flashと同等以上の性能があるように感じました。

### AIによる参照をブロックする設定方法

症例データなどAIに見られたくないファイルは、AIがアクセスできないようブロックする必要があります。  
設定方法を説明します。  
① \`.aiexclude\`というファイル（拡張子なし）を作成

![画像](https://assets.st-note.com/img/1751939625-6c4DNiHEbRXduYS5hkAZWvrK.png)

② \`.aiexclude\`を開いてブロックしたいファイルやフォルダを指定  
　 Gitを使っている方は.gitignoreと同じ記法でOKです。

![画像](https://assets.st-note.com/img/1751939912-C43zd5Il0V6noxcmypD8Nfhg.png)

![画像](https://assets.st-note.com/img/1751939787-3fVEoIZ2yCP4uaGt1N90lUDH.png?width=1200)

以上です。Cursorでいう\`.cursorignore\`の使い方と全く同じです。  
ちなみに、VS Codeの設定画面で\`Geminicodeassist: Context Exclusion File\`と検索してヒットする項目で、この設定ファイル名を変更することもできます。

![画像](https://assets.st-note.com/img/1751939506-PWKLalnZHAQS3tGE5uJjigU1.png)

### プライバシー設定

チャットペインの右上の「…」を選択し「Privacy settings」を選択します。

![画像](https://assets.st-note.com/img/1751931927-IQPm35GouJaDV7NtqX9LhHMd.png?width=1200)

すると下の画面のようにプライバシーに関する通知が表示されます。

![画像](https://assets.st-note.com/img/1751932101-yYHFmnIuAxMbepac4zCSh5r9.png?width=1200)

🔲のチェックを外すことで、プライバシー設定を強化することができます。  
和訳したものはコチラ👇

> **個人向けGemini Code Assist プライバシー通知**  
> この通知とGoogleのプライバシーポリシー（ [https://policies.google.com/privacy）では、個人向けGemini](https://www.google.com/url?sa=E&q=https%3A%2F%2Fpolicies.google.com%2Fprivacy%EF%BC%89%E3%81%A7%E3%81%AF%E3%80%81%E5%80%8B%E4%BA%BA%E5%90%91%E3%81%91Gemini) Code Assistがお客様のデータをどのように取り扱うかについて説明しています。内容をよくお読みください。  
> お客様が個人向けGemini Code Assistを使用すると、Googleは、Googleのプロダクトやサービス、機械学習技術の提供、改善、開発を目的として、お客様の **プロンプト、関連するコード、生成された出力、コードの編集内容、関連機能の利用情報、およびお客様のフィードバックを収集します。**  
> 品質の維持とプロダクト（生成AIモデルなど）の改善のため、人間のレビュー担当者が、上記で収集されたデータを閲覧、注釈付け、処理することがあります。このプロセスの一環として、Googleはお客様のプライバシーを保護するための措置を講じています。これには、レビュー担当者がデータを閲覧または注釈付けを行う前に、お客様のGoogleアカウントからデータを切り離すことや、その切り離されたコピーを最大18か月間保管することが含まれます。機密情報や、レビュー担当者による閲覧、あるいはGoogleによるプロダクト、サービス、機械学習技術の改善目的での利用を希望しないデータは、送信しないようお願いいたします。  
> このデータがGoogleの機械学習モデルの改善に使用されることを希望しない場合は、以下でオプトアウト（無効化）できます。  
> **🔲 Googleがこのデータを使用してGoogleの機械学習モデルを開発および改善することを許可する。**  
> 注：この設定が反映されるまで、最大1分かかる場合があります。

チェックを外した場合（オプトアウト）は以下のようになると思われます。

> サービスの提供（プロンプトの処理、応答の生成など）： **行われる**  
> 機械学習モデルの改善・開発のためのデータ利用： **行われない**  
> 上記目的のための人間によるレビュー： **行われない**

ただしプライバシー設定の✅を外した場合でも、以下の行為は必ず避けるようにしてください

- 機密情報をプロンプトで送信
- 機密情報が含まれるファイルを参照させる

## まとめ

Gemini Code AssistにはCursorとほとんど同じ機能が用意されています。  
長所は何と言っても「無料でほぼ無制限に使える」ことです。

もちろんCursorの方が優れてる部分もたくさんあります。

- 有料版で使えるAIモデル (Claude 4, o3など)
- 自動コード補完機能
- コードベースの理解

ただデータ分析用途では巨大なコードベースを扱ったりしないので、Gemini Code Assistで十分だと感じたのもまた事実。

### こんな人におすすめ

- 無料でAIコーディングアシストを使いたいひと
- Cursorの無料プランを使っていた人

使ってみた感じのAI性能を比較すると  
Cursor（課金） > Gemini Code Assist ≒ Cursor（無課金）です。  
Gemini Code Assistは無料でGemini 2.5モデルが使えて、さらに240回/日のチャットリクエスト枠が使えるのが大きなメリットです。

### 逆にこんな人にはおすすめしない

AI駆動開発などでCursorの有料版をゴリゴリ使っている方や、Claude CodeをMAXプランでぶん回している方はGemini Codo Assistの自走力はもの足りないでしょう。

無料で優秀なGeminiが使えるのは多くの人にとって代えがたい魅力だと思います。また天下のGoogleなので今後さらに伸びていく可能性も充分あると思います。

## 最後に：𝕏, Noteのフォローお願いします！

この記事が役に立ったと思っていただけたら幸いです。  
  
𝕏では研究に役立つTipsやAI活用の最新情報を発信しています。よろしければ [こちらからフォロー](https://x.com/ai_biostat) いただけると嬉しいです。

Noteは𝕏に収まりきらないコンテンツや𝕏で反応が良かったものをより詳細に発信しています。「スキ」ボタン、フォローいただけると励みになります。

Cursorよりおすすめ？『Gemini Code Assist』の使い方｜わたヤク