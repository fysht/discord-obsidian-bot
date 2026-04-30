"""VAPID 鍵ペアを生成して .env 形式で標準出力するスクリプト。

使い方:
    1. venv を有効化した状態で実行: python scratch/gen_vapid.py
    2. 出力された3行を .env にコピーする
    3. 既に鍵がある場合は再生成しないこと（既存購読が無効化されるため）

依存:
    pip install cryptography
"""

import base64
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def main() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()

    private_numbers = private_key.private_numbers()
    private_value = private_numbers.private_value.to_bytes(32, "big")

    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )

    private_b64 = _b64url(private_value)
    public_b64 = _b64url(public_bytes)

    print("# 以下3行を .env に貼り付けてください")
    print("# (subject はメールアドレスに変更可能)")
    print(f"VAPID_PUBLIC_KEY={public_b64}")
    print(f"VAPID_PRIVATE_KEY={private_b64}")
    print("VAPID_SUBJECT=mailto:f.yshx.117@gmail.com")


if __name__ == "__main__":
    main()
