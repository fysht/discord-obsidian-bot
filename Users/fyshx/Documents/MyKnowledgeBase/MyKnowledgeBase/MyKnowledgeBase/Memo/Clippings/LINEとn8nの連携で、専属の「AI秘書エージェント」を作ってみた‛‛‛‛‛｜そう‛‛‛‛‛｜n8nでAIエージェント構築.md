---
title: "LINEとn8nの連携で、専属の「AI秘書エージェント」を作ってみた｜そう｜n8nでAIエージェント構築"
source: "https://note.com/soh_ainsight/n/n4d6d57666917"
author:
  - "[[そう｜n8nでAIエージェント構築]]"
published: 2025-06-11
created: 2025-08-09
description: "毎日のスケジュール管理やタスクの整理に追われていませんか？メールやSlackなどで届く予定調整依頼などで、大切な時間を奪われている方もいらっしゃるのではないでしょうか？  「予定あいてますか？」といった質問に、いちいち手動で確認して返信するのは面倒ですよね。また、予定の追加や変更の依頼も、LINEで受け取った後、手動でカレンダーに反映するのは手間がかかります。  しかし、n8nとLINEを連携させたAI秘書エージェントを使えば、これらの作業を自動化できます。LINEで受け取ったメッセージを自動で処理し、予定の確認やカレンダーへの予定追加まで、すべて自動で行ってくれるのです。  この記事"
tags:
  - "clippings"
---
![見出し画像](https://assets.st-note.com/production/uploads/images/195463665/rectangle_large_type_2_be89c7262a91828717b9b6b53a31fad8.png?width=1200)

## LINEとn8nの連携で、専属の「AI秘書エージェント」を作ってみた

[そう｜n8nでAIエージェント構築](https://note.com/soh_ainsight)

毎日のスケジュール管理やタスクの整理に追われていませんか？メールやSlackなどで届く予定調整依頼などで、大切な時間を奪われている方もいらっしゃるのではないでしょうか？

「予定あいてますか？」といった質問に、いちいち手動で確認して返信するのは面倒ですよね。また、予定の追加や変更の依頼も、LINEで受け取った後、手動でカレンダーに反映するのは手間がかかります。

しかし、n8nとLINEを連携させたAI秘書エージェントを使えば、これらの作業を自動化できます。 **LINEで受け取ったメッセージを自動で処理し、予定の確認やカレンダーへの予定追加まで、すべて自動** で行ってくれるのです。

この記事では、プログラミングの知識がなくても作れる、LINEとn8nを連携させたAI秘書エージェントの構築方法を解説します。

  

## 実現できること

![画像](https://assets.st-note.com/img/1749514112-1DKfrvquOey3VjxLk28YB7Zm.png?width=1200)

AI秘書エージェントは以下の機能を実現できます。

> **✅できること**  
> ・LINEで受け取った予定の確認依頼に自動応答  
> ・予定の追加をカレンダーに自動反映  
> ・自然な会話形式での操作が可能

  

## AI秘書エージェントの仕組み

### n8nとは

![画像](https://assets.st-note.com/img/1749615160-8o2zF7AMORnYdaqDL6UmkcNW.png?width=1200)

n8n（エヌエイトエヌ）は、プログラミングの知識がなくても、様々なアプリやサービスを簡単に連携できる「自動化ツール」です。

例えば、LINEで受け取ったメッセージを自動で処理して、Googleカレンダーに予定を追加したり、天気予報を取得して返信したりといった作業を自動化できます。

[**n8n.io - a powerful workflow automation tool** *n8n is a free and source-available workflow automation tool* *n8n.partnerlinks.io*](https://n8n.partnerlinks.io/jglfra1r0rtp)

n8nの特徴は以下の3つです。

1. **ノーコードで使える**
	- プログラミングの知識が不要
	- マウスでドラッグ＆ドロップするだけで設定可能
	- 直感的な操作で自動化の仕組みを作れる
2. **豊富な連携機能**
	- LINE、Google、Slackなど、400以上ののサービスと連携可能
	- 各サービスを「ノード」という部品で簡単に接続
	- 複雑な処理も視覚的に組み立てられる
3. **無料で始められる**
	- クラウド版は無料プランから利用可能
	- オープンソース版は自分でサーバーにインストール可能
	- 初心者でも気軽に試せる

  

### Webhookとは

Webhook（ウェブフック）は、アプリケーション間で情報を自動的にやり取りする仕組みです。LINEとn8nを連携させる際の「架け橋」として機能します。

例えば、あなたがある商品の在庫を確認したいときは、お店で店員さんに「在庫はありますか？」と聞くことで、答えを知ることができます。ただ、この場合、在庫があるかどうか聞かなければならず、面倒くさいです。

一方、そのお店にて会員登録しておくと、商品が入荷したときに自動的に「商品が入荷しました！」と通知が来るようになりました。Webhookは、この仕組みによく似ています。

> **✅Webhookのメリット**  
> ・リアルタイムで情報をやり取りできる  
> ・手動での操作が不要  
> ・24時間365日、自動で動作する

n8nとWebhookによる情報伝達の流れを簡単にお伝えすると以下のようになります。

1. **LINEからn8nへの通知**
	- ユーザーがLINEでメッセージを送信
	- LINEが「新しいメッセージが届きました」とn8nに通知
	- n8nがその通知を受け取って処理を開始
2. **n8nでの処理**
	- 受け取ったメッセージの内容を確認
	- 必要な情報（予定や天気など）を取得
	- 応答メッセージを作成
3. **n8nからLINEへの返信**
	- 作成した応答メッセージをLINEに送信
	- ユーザーに自動で返信が届く

  

## AI秘書エージェントを構築する

### 【Step.1】n8nのセットアップ

![画像](https://assets.st-note.com/img/1748961713-sJKLaIAzfjBCxrQeV458mbWY.png?width=1200)

n8nのセットアップは、AI秘書エージェント構築の第一歩です。クラウド版n8nを使用することで、サーバーの設定や管理の手間を省き、すぐに開発を始めることができます。

まずは、n8nの公式サイトにアクセスしてアカウントを作成しましょう。サイト右上の「Get Started」ボタンをクリックすると、サインアップ画面が表示されます。

メールアドレスとパスワードを入力し、確認メールのリンクをクリックすることで、アカウントの作成が完了します。

![画像](https://assets.st-note.com/img/1748961741-HFZJesujfOAxkKChXSgdpB6Q.png?width=1200)

アカウント作成後、ワークスペースの設定を行います。画面上部の「＋ Create Workflow」ボタンをクリックすると、新しいワークフローが作成されます。右上のワークフロー名を「AI秘書エージェント」など、目的が分かりやすい名前に変更しましょう。

> **✅確認ポイント**  
> ・アカウントが正常に作成できたか  
> ・確認メールが届き、リンクをクリックしたか  
> ・ワークフローが正しく作成できたか  
> ・ワークフロー名が適切に設定されているか  

  

### 【Step.2】LINE公式アカウントの作成

LINEとの接続設定は、AI秘書エージェントの重要な基盤となります。LINE Developersコンソールでチャネルを作成し、n8nのWebhookと連携させることで、LINEからのメッセージを自動処理できるようになります。

はじめに、LINE公式アカウントを開設しましょう。LINE公式アカウントとは、「LINE」上で企業や店舗がアカウントをつくり、友だち追加してくれたユーザーに直接情報を届けることができるサービスです。

[**LINE公式アカウントをはじめよう｜アカウント作成の流れ** *LINE公式アカウントの作成の流れをご紹介。かんたん3STEPで、PCまたはスマートフォンより簡単に作成いただけます。LI* *entry.line.biz*](https://entry.line.biz/start/jp/)

LINE公式アカウント [管理画面](https://www.lycbiz.com/jp/login/) にログイン後、「設定」をクリック。左メニューの「Messaging APIをする」をクリックし、開発者情報※を入力してください。

※LINE公式アカウント作成時の名前やメールアドレスと同じで大丈夫です。

![画像](https://assets.st-note.com/img/1748962296-qQaSl1ATEftHvwKyz8LnePOo.png?width=1200)

  

### 【Step.3】プロバイダーの登録

![画像](https://assets.st-note.com/img/1748962521-W9eqbJzMGmtnkLcBaTU6sSlD.png?width=1200)

プロバイダーを作成してください。LINE公式アカウント作成時の名前と同じで大丈夫です。その後は、内容を読んで登録を進めてください。

プロバイダー名は任意です。ご自身があとて見返したときに、何目的で作ったものだったのかが判別できれば問題ありません。

プロバイダー名：AI秘書エージェント

  

### 【Step.4】LINE接続のセットアップ

![画像](https://assets.st-note.com/img/1748962796-X8m5blZev0gFnaUxtETWDj3d.png?width=1200)

続いて、 [LINE Developersコンソール](https://developers.line.biz/ja/) にアクセスし、先ほど作成したプロバイダを選択してください。

チャネル基本設定の下部にある「チャネルシークレット」のコードをコピーしてください。チャネルアクセストークンは、後でn8nの設定で使用するため、必ずコピーして安全な場所に保存してください。

![画像](https://assets.st-note.com/img/1748963010-VG7dBUqn4sPSIOQJv98XWzxY.png?width=1200)

  

### 【Step.5】n8nワークフローの作成

![画像](https://assets.st-note.com/img/1748963206-MCvxFiKyYG4wSDg9HVz8QPkl.png?width=1200)

次に、n8nに戻って、ワークフローにWebhookノードを追加します。「Add first step」をクリックして「webhook」で検索しましょう。

![画像](https://assets.st-note.com/img/1748963667-n6S52IqFeJak8g3xtUy0vXwM.png?width=1200)

ノードの設定で、HTTP Methodを「POST」に設定します、Pathには任意の文字列（例：\`/line-n8n\`）を入力します。

生成されたWebhook URLをコピーし、LINE公式アカウント管理画面のWebhook設定に貼り付けます。「設定」＞「Messaging API」で先ほど表示された画面です。

![画像](https://assets.st-note.com/img/1748963713-eEaBIZCJVQkPO187izh0Do6y.png?width=1200)

続いて、 [LINE Developersコンソール](https://developers.line.biz/ja/) にアクセスし、先ほど作成したプロバイダを選択してください。

「Messaging API設定」タブに移動し、下にスクロールすると「Webhook設定」の項目にある「 **Webhookの利用** 」があるので、こちらをONにします。そして、「 **Webhook URL** 」直下にある「検証」ボタンもクリックしてください。

「検証」クリックしたあとに「成功」と表示されれば成功です。これにより、LINE側でユーザーからのメッセージを受け取り次第、その情報をn8nに接続する仕組みが完成します。

![画像](https://assets.st-note.com/img/1748964146-0goKTr8JYfhQCz94jXAtHS1w.png?width=1200)

n8nに戻って、Webhookトリガーを開き、「Listen for test event」を押します。すると「Listening for test event」という状態に切り替わるので、LINE公式アカウント宛てに適当な文章を送ってみましょう。

n8n側でLINEのメッセージを受け取れると、右側のOUTPUTにLINE側で送ったメッセージを確認することができます。

![画像](https://assets.st-note.com/img/1748964618-mxsBMOnFfr9AzP7SkL4CwH6l.png?width=1200)

  

### 【Step.6】AIエージェントノードを設定する

![画像](https://assets.st-note.com/img/1748992880-sUry1A4MawF5vgqdSul6HbR0.png?width=1200)

続いて、webhookで取得したデータの中から、LINEメッセージにあたる値だけを抽出する処理を追加していきます。

「Webhook」の右側に伸びる「+」をクリックして「Edit Fields」と検索して「Edit Fields (Set)」を選択しましょう。

「INPUT」の中からLINEメッセージにあたる部分を見つけて、「Drag input fields here」にドラッグアンドドロップしましょう。この設定により、LINEメッセージだけ取得できるようになります。

![画像](https://assets.st-note.com/img/1748993078-SwhZvPOFRMysxYI0n48r9aTz.png?width=1200)

  

### 【Step.7】AIエージェントノードを設定する

![画像](https://assets.st-note.com/img/1748993235-7Hr1gA4xvOQ9ypqN0MewcE8m.png?width=1200)

「Edit Fields」ノードの後に、新しいノードを追加します。カテゴリ一覧から「Advanced AI」を開き、「AI Agent」ノードを選択してください。「ai agent」で検索しても表示できます。

続いて、AIエージェントノードの設定画面が表示されるので、AIエージェントに与える指示（Prompt）を入力します。

「Source for Prompt」（プロンプトの入力元）は、「Define Below」（ここに直接入力）に切り替えて、プロンプトを入力しましょう。

> **Prompt**  
> あなたは優秀なAIエージェント秘書です。本日の日付を取得したあとに、ユーザーからの質問に対して、以下の情報を元に適切に応答してください。  
>   
> 1\. カレンダーの予定情報  
> 2\. 一般的な質問への回答  
>   
> 応答は簡潔で分かりやすく、一文で回答してください。  
>   
> \## ユーザーからの質問  
> {{ $json.body.events\[0\].message.text }}

※ユーザーからの質問の部分には、「INPUT」のLINEメッセージに相当する部分をドラッグアンドドロップしてください。

  

### 【Step.8】「OpenAI Chat Model」を開く

![画像](https://assets.st-note.com/img/1748965281-RGozpdtqF354WgXeU8KNsQV9.png?width=1200)

続いて、n8nの画面に戻り、「OpenAI Model」の下の「＋」をクリックして、「OpenAI」と検索しましょう。「OpenAI Chat Model」を開いて、「Create new credential」をクリックしていくことでAPIの設定を進めます。

OpenAIのアカウント画面が開くので、「API key」の部分に先ほどOpenAI プラットフォームで発行したAPIキーを貼り付けます。ほかの設定画面はいじる必要はありません。

![画像](https://assets.st-note.com/img/1748965243-OUySiZtJkxl6RshdbYuoN8w4.png?width=1200)

  

### 【Step.9】「Google calendar」を設定する

![画像](https://assets.st-note.com/img/1748993926-3umfMaBxX8qowjsRZJ4kyDp5.png?width=1200)

続いて、「AIエージェント」ノードから下に伸びる「Tool」をクリックします。「Google calendar」を探してクリックしましょう。

「Create new credential」をクリックして参照したいGoogleカレンダーのGoogleアカウントでログインして接続してください。Parametersの設定は以下を参考にしてください。

**Google Calendar**  
Resource：Event  
Operation：Get Many  
From list：ご自身のカレンダーを選択  
Limit：50  
After：星のマークをクリック  
Before：星のマークをクリック

この設定にて、Googleカレンダーから必要なカレンダー情報を取得する設定を行いました。

![画像](https://assets.st-note.com/img/1748996021-j82S3t1d6RseJkxwXpYGc0VM.png?width=1200)

再度、「AIエージェント」ノードから下に伸びる「Tool」をクリックします。「Google calendar」を探してクリックしましょう。

先ほど、Googleカレンダーから情報を取得する設定を行いましたが、今度は、カレンダー登録する権限を付与していきます。

**Google Calendar**  
Resource：Event  
Operation：Create  
From list：ご自身のカレンダーを選択  
Limit：50  
Start：星のマークをクリック  
End：星のマークをクリック

最後に、Additional Fieldsから「Summary」を追加して星のマークをクリックしましょう。この星マークは、LLMによって自動生成しますよという意味です。

![画像](https://assets.st-note.com/img/1748996121-Rce8TLW94jsoMOxDiSCaPYlQ.png?width=1200)

  

### 【Step.10】「Date & Time」を追加する

![画像](https://assets.st-note.com/img/1748996403-XDchUnwu60ZF9laBg2PMH1Li.png?width=1200)

n8nでは、「今日」や「now」という指示が必ずしもワークフロー実行時点のn8nサーバーの時刻になるとは限らず、意図しない日時になることがあります。

その問題を解消するため、AIエージェントノードから伸びるToolの「+」をクリックして「Date & Time」を設定しましょう。これで、今日がいつかを取得してくれます。※「Date & Time」の名前のままだとエラーでたので「Date\_Time」にリネームしました。

**Date & Time**  
Tool Description：Set Automatically  
Operation：Get Current Date  
Include Current Time：Defined automatically by the model  
Output Field Name：Defined automatically by the model

  

### 【Step.11】LINEへの返信設定

最後に、AI秘書エージェントからの応答をLINEに送信する設定を行います。n8nのワークフローにHTTPリクエストノードを追加し、LINE Messaging APIの設定を行います。

「AIエージェントノード」の右側にある「+」をクリックしてHTTPリクエストノードを追加しましょう。「http request」と検索すると出てきます。

**HTTPリクエストノード**  
Method: POST  
URL: [https://api.line.me/v2/bot/message/reply](https://api.line.me/v2/bot/message/reply)  
Authentication：Generic Credential Type  
Generic Auth Type：Header Auth  
Header Auth：以下の画像のように設定行ってください

![画像](https://assets.st-note.com/img/1749012297-JczFbAa92YsKyHLIMkD3tiTQ.png?width=1200)

Send Headers：ON  
Specify Headers：Using Fields Below  
Header Parameters  
Name：Content-Type  
Value：application/json

Send Body：ON  
Body Content Type：JSON  
Specify Body：Using JSON  
JSON：以下の内容を追加ください。

![画像](https://assets.st-note.com/img/1749081011-t3cGov9DJXSpZ2jkB0UQnVRr.png?width=1200)

> **JSON**  
> {  
> "replyToken": "{{ $('Webhook').item.json.body.events\[0\].replyToken }}",  
> "messages": \[{  
> "type": "text",  
> "text": "{{ $json.output }}"  
> }\]  
> }

  

## まとめ

![画像](https://assets.st-note.com/img/1749012590-E3qW2X9i1FDczMfogL8AbjhH.png?width=1200)

n8nとLINEを連携させることで、日常的な予定管理や情報収集を自動化できるAI秘書エージェントを構築できました。この記事で解説した方法を参考に、自分だけのAI秘書エージェントを作ってみてください。

ただし、AIエージェントの応答は完璧ではありません。重要な予定の確認や変更は、必ず人間が最終確認することをお勧めします。また、プライバシーに配慮し、個人情報の取り扱いには十分注意してください。

初心者の方は、まず基本的な機能から実装を始めて、徐々に機能を追加していくことをおすすめします。n8nの可能性は広がっており、AI秘書エージェントの機能をさらに拡張することも可能です。

LINEとn8nの連携で、専属の「AI秘書エージェント」を作ってみた｜そう｜n8nでAIエージェント構築