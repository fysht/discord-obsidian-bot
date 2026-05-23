"""api.routers: routes.py を機能単位で分割していくサブパッケージ。

このディレクトリの目的は、4700行ある api/routes.py を段階的に
機能ドメインごと（investment, tasks, habits, gmail...）の独立ルーターへ
切り出すこと。各モジュールは APIRouter インスタンスを export し、
main.py の include_router で接続される。
"""
