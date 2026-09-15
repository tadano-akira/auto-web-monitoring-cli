# auto-web-monitoring-cli

ヘッドレスChromeでJavaScriptレンダリング後のWebページを定期取得し、
内容の変化を検知して、指定キーワードを含む場合のみメールで通知するCLIツール。
複数の監視対象をひとつの設定ファイルで管理できる。

## 特徴

- `--headless --dump-dom` によりJavaScriptで描画されるページも正しく取得する
- 前回取得分とのSHA256ハッシュ比較でページ更新を検知する
- 更新があってもキーワードに一致しない限り通知しない
- 同一日内に一度通知したら、その日はそれ以上通知しない(連続通知の抑制)
- 監視対象ごとに取得間隔・監視時間帯・キーワード・通知先メールアドレスを個別設定できる
- SMTP送信はBrevo(旧Sendinblue)を想定(他のSMTPサーバーでも設定変更のみで利用可能)
- Python標準ライブラリ+PyYAMLのみで動作し、systemdでLinuxサーバーに常駐させて24/365稼働できる

## 動作の仕組み

各監視対象に対して、次の手順で1サイクルのチェックを行う。

1. 監視時間帯(`time_window`)外なら何もしない
2. 前回チェックから `interval_minutes` 分経過していなければ何もしない
3. `google-chrome --headless --dump-dom --virtual-time-budget=...` でレンダリング後のDOMを取得する
4. 取得したHTMLのSHA256ハッシュを、前回保存分と比較する
   - 初回取得時はベースラインとして保存するのみで、通知判定は行わない
   - ハッシュが一致すれば「変更なし」として終了する
5. ハッシュが変化していれば「更新あり」と判定し、`keyword` がHTML本文に含まれるか調べる
   - 含まれていなければ通知せず、ハッシュのみ更新する
   - 含まれていれば、その監視対象の当日分の通知をまだ送っていない場合に限りメール送信する
   - 送信済みかどうかは監視対象ごとに `last_notified_date`(日付)で判定するため、
     同じ日に何度更新を検知しても通知は最大1通に抑えられる

デーモンは `check_interval_sec` 秒ごとに全監視対象をスキャンし、
上記のうち「時間帯内」かつ「間隔経過済み」の監視対象だけを実際にチェックする。

## 必要環境

- Python 3.10以上(`list[Monitor]` 等のPEP 585構文を使用)
- Google Chrome または Chromium(`--headless --dump-dom` に対応するバージョン)
- Brevo(または他の)SMTPアカウント
- (Linuxサーバーで常駐させる場合)systemd

## セットアップ

```bash
git clone https://github.com/tadano-akira/auto-web-monitoring-cli.git
cd auto-web-monitoring-cli

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp monitoring.example.yaml monitoring.yaml   # 監視対象を編集する
cp .env.example .env                          # SMTPパスワードを設定する
chmod 600 .env
```

## 設定ファイル (`monitoring.yaml`)

### `smtp` — SMTP設定

| キー | 必須 | 説明 |
|---|---|---|
| `host` | ○ | SMTPホスト名。Brevoの場合 `smtp-relay.brevo.com` |
| `port` | ○ | SMTPポート番号。BrevoのSTARTTLSは `587` |
| `user` | ○ | SMTPログインユーザー(Brevoの場合SMTPキーに紐づくログイン用アドレス) |
| `password_env` | ○ | SMTPパスワードを読み込む環境変数名。値そのものはYAMLに書かない |
| `from_addr` | ○ | 送信元メールアドレス |

### `chrome` — ヘッドレスChrome設定

| キー | 必須 | デフォルト | 説明 |
|---|---|---|---|
| `binary` | - | `google-chrome` | Chrome/Chromiumの実行ファイル名またはフルパス |
| `virtual_time_budget_ms` | - | `10000` | `--virtual-time-budget` に渡す値(ミリ秒)。JSの非同期描画待ち時間 |
| `timeout_sec` | - | `30` | Chromeプロセスのタイムアウト秒数 |
| `extra_args` | - | `[]` | Chromeに追加で渡すコマンドライン引数のリスト(例: `--no-sandbox`) |

### トップレベル

| キー | 必須 | デフォルト | 説明 |
|---|---|---|---|
| `check_interval_sec` | - | `60` | デーモンが全監視対象をスキャンする間隔(秒) |
| `monitors` | ○ | - | 監視対象のリスト(後述) |

### `monitors[]` — 監視対象ごとの設定

| キー | 必須 | デフォルト | 説明 |
|---|---|---|---|
| `id` | ○ | - | 監視No。状態ファイル名 `state/monitor_<id>.json` に使われる一意な整数 |
| `name` | - | `監視No.<id>` | 通知メールに表示する監視名 |
| `url` | ○ | - | 監視対象URL |
| `interval_minutes` | - | `5` | チェック間隔(分) |
| `time_window.start` / `time_window.end` | - | 制限なし | 監視する時間帯。`HH:MM` の24時間表記。省略すると終日監視する |
| `keyword` | ○ | - | このキーワードが本文(取得HTML全体)に含まれていたら通知する部分一致・大文字小文字を区別する文字列 |
| `notify_to` | ○ | - | 通知先メールアドレス |

`time_window` は `start > end` (例: `22:00`〜`06:00`)を指定すると日をまたぐ時間帯としても扱われる。

設定例は [`monitoring.example.yaml`](monitoring.example.yaml) を参照。

## 環境変数 (`.env`)

`monitoring.yaml` の `smtp.password_env` で指定した名前の環境変数にSMTPパスワードを設定する。
デフォルトの例では `BREVO_SMTP_PASSWORD` を使用する。

```
BREVO_SMTP_PASSWORD=your-brevo-smtp-key
```

`.env` はsystemdの `EnvironmentFile=` から読み込まれる。手元のシェルから直接実行してテストする場合は、
自分でexportする必要がある。

```bash
set -a && source .env && set +a
```

## 実行方法

```bash
# デーモンとして起動(Ctrl+Cで停止)
.venv/bin/python monitor.py --config monitoring.yaml

# 1回だけ全監視対象をチェックして終了する(cron運用やテスト向け)
.venv/bin/python monitor.py --config monitoring.yaml --once

# メール送信せずログのみ出力する(動作確認用)
.venv/bin/python monitor.py --config monitoring.yaml --once --dry-run
```

| オプション | 説明 |
|---|---|
| `--config PATH` | 設定ファイルのパス。省略時はスクリプトと同じディレクトリの `monitoring.yaml` |
| `--once` | デーモンループせず1回だけ全監視対象をチェックして終了する |
| `--dry-run` | 通知条件に一致してもメール送信を行わず、ログ出力のみで確認する |

## systemdでの常駐化

`systemd/web-monitor.service` にユニットファイルのテンプレートを同梱している。
`WorkingDirectory` / `EnvironmentFile` / `ExecStart` のパスと `User` を実際の配置環境に合わせて編集し、
以下の手順で登録する。

```bash
sudo cp systemd/web-monitor.service /etc/systemd/system/web-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now web-monitor

# 状態確認
sudo systemctl status web-monitor

# 設定変更後の反映
sudo systemctl restart web-monitor
```

`Restart=always` を設定しているため、プロセスが異常終了しても自動再起動する。

## ログと状態ファイル

- ログ: `logs/monitor.log`(5MBごとにローテーション、最大5世代保持)。標準出力にも同時出力する
- 状態: `state/monitor_<id>.json` に監視対象ごとの `last_hash` / `last_checked_at` / `last_notified_date` を保存する
  - このファイルを削除するとベースラインがリセットされ、次回チェックが「初回取得」扱いになる(通知は行われない)
  - `last_notified_date` を手動で書き換える、またはファイルごと削除すると、その日の通知抑制を解除できる

## 注意事項・既知の制限

- キーワード判定は取得したHTML全体に対する単純な部分一致(大文字小文字を区別)。正規表現や除外条件には対応していない
- 差分検知はページ全体のハッシュ比較のみで、変更箇所の可視化(差分表示)は行わない
- 監視対象の `url` や `id` を設定変更した場合、直前の `last_hash` と比較対象が変わるため、
  変更直後の1回は「更新あり」と誤検知しうる。設定変更時は該当 `state/monitor_<id>.json` を削除してから再起動することを推奨する
- 時間帯・日付の判定はすべてサーバーのローカルタイムゾーンに従う

## トラブルシューティング

| 症状 | 確認事項 |
|---|---|
| ログに `chrome headless failed` と出る | `chrome.binary` のパスが正しいか、対象サーバーにChromeがインストール済みか確認する |
| 通知メールが届かない | `logs/monitor.log` にSMTPエラーが出ていないか確認する。`.env` の環境変数名が `password_env` と一致しているか確認する |
| 更新は検知するがメールが来ない | `keyword` が実際のページ本文(レンダリング後DOM)に含まれているか確認する。同日中に一度通知済みでないか `state/monitor_<id>.json` の `last_notified_date` を確認する |
| 想定の時間帯でチェックされない | サーバーのタイムゾーンと `time_window` の設定がずれていないか確認する |
