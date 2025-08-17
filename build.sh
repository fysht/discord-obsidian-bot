# -eオプションは、コマンドが一つでも失敗したら即座にスクリプトを終了させる設定です
set -e

# Pythonの依存関係をインストール
pip install -r requirements.txt

# rcloneのインストール
echo "Installing rclone..."
mkdir -p bin
curl -L https://downloads.rclone.org/rclone-current-linux-amd64.zip -o /tmp/rclone.zip
unzip -q /tmp/rclone.zip -d /tmp
cp /tmp/rclone-*/rclone ./bin/rclone
chmod +x ./bin/rclone
echo "rclone installed successfully."

# rcloneの設定ファイルを環境変数から生成
echo "Creating rclone config..."
mkdir -p /opt/render/.config/rclone
echo "$RCLONE_CONFIG_BASE64" | base64 --decode > /opt/render/.config/rclone/rclone.conf
echo "rclone config created successfully."

echo "Build script finished."