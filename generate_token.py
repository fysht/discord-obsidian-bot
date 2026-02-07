import os
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import logging

# --- 設定 ---
CREDENTIALS_FILE = 'credentials.json'
TOKEN_FILE = 'token.json'

# --- スコープ設定 ---
# 修正: Google Drive API (読み書き権限) を追加しました
SCOPES = [
    'https://www.googleapis.com/auth/calendar',   # Calendar Cog用
    'https://www.googleapis.com/auth/documents',  # Google Docs Handler用
    'https://www.googleapis.com/auth/drive'       # Google Drive Sync用 (★追加)
]
# --- ここまで ---

def main():
    """
    Googleの認証フローを実行し、token.jsonを生成・更新します。
    """
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    creds = None
    
    # 既存のトークンファイルがある場合
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            logging.info(f"{TOKEN_FILE} を読み込みました。")
        except ValueError:
             # スコープが変更された場合などはここに来る可能性がある
             logging.warning(f"既存の {TOKEN_FILE} のスコープが不足しているか不一致のため、再認証を行います。")
             creds = None
        except Exception as e:
            logging.warning(f"既存の {TOKEN_FILE} の読み込みに失敗しました: {e}")
            creds = None

    # 有効な認証情報がない場合、リフレッシュまたは新規取得を行う
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                logging.info("トークンが期限切れです。リフレッシュを試みます...")
                creds.refresh(Request())
                logging.info("トークンのリフレッシュに成功しました。")
            except Exception as e:
                logging.warning(f"トークンのリフレッシュに失敗しました: {e}")
                creds = None # リフレッシュ失敗時は新規取得へ
        
        if not creds:
            logging.info("新しい認証フローを開始します。ブラウザで認証してください...")
            if not os.path.exists(CREDENTIALS_FILE):
                 logging.error(f"{CREDENTIALS_FILE} が見つかりません。Google Cloud Consoleからダウンロードして配置してください。")
                 return
            
            try:
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                # ローカルサーバーを立ち上げて認証 (ポート0は空きポートを自動選択)
                creds = flow.run_local_server(port=0)
            except Exception as e:
                 logging.error(f"認証フローの実行中にエラーが発生しました: {e}")
                 return

    # 認証情報の保存
    if creds:
        # スコープの確認ログ
        logging.info(f"取得/更新された認証情報のスコープ: {creds.scopes}")
        
        try:
            with open(TOKEN_FILE, 'w') as token:
                token.write(creds.to_json())
            logging.info(f"'{TOKEN_FILE}' が正常に作成・更新されました。")
            logging.info("--> このファイルの中身をRenderの環境変数 'GOOGLE_TOKEN_JSON' に設定してください。")
        except Exception as e:
            logging.error(f"ファイル書き込みエラー: {e}")
    else:
         logging.error("認証情報の取得に失敗したため、token.json は更新されませんでした。")


if __name__ == '__main__':
    main()