# homelab-backup 止血部署清單（fix/stop-the-bleeding）

> 給主機管理者用 `sudo` 執行。本文件不含任何秘密；通知 URL 只寫在
> root 擁有、mode 0600 的檔案裡，**不要**貼進 repo、config.yaml 或聊天。
> 以下 `<repo-dir>` 是本 repo 的 checkout 路徑，`<service>` 是服務 ID，
> `<container>` 是 Docker 容器名稱。

## 0. 背景：為什麼備份會長期沒有成功

這個分支修正的是幾個會疊在一起、而且彼此不會告警的失敗模式
（可用 `journalctl -u homelab-backup.service` 比對）：

| 症狀 | 原因 | 對應修正 |
|---|---|---|
| `restic` exit code 11、`error while unlocking: context canceled` | repository 留下過期的 restic lock，之後每次 backup／forget 都拿不到 lock | 取得 flock 後先跑 `restic unlock`（不加 `--remove-all`，只清掉 restic 判定過期的 lock） |
| `docker run ... sha256:<image-id>` exit code 125 | helper image 被 `docker image prune -a` 之類的指令清掉；舊 launcher 用 image ID 釘死，且每次備份前都跑 bind probe，所有 Docker 服務在 staging 前就失敗 | 拿掉 bind probe，改用 tag；helper 不存在時用 release 內附的 `Dockerfile.volume-rsync` 自動重建 |
| before action 的 `docker exec <container> ...` exit 1 | 該容器已被移除或停止 | backup action 可加 `skip_if_not_running`（需要改 manifest，見步驟 3） |
| 失敗沒有任何通知 | systemd unit 沒有 `OnFailure=`，`status` 也不檢查太久沒成功 | 所有 service 加 `OnFailure=homelab-backup-notify@%n.service`；`status` 檢查新鮮度與卡住的全域鎖並回傳 exit 2 |

另外：備份後不再跑 forget（retention 只在每週 maintenance 進行），restic check
從 maintenance 的 `ExecStopPost` 拆成獨立的 `homelab-backup-check.service`／timer。

## 1. 備份現行安裝並停用排程（必做）

```bash
TS=$(date +%Y%m%d-%H%M%S)
B=/root/homelab-backup-pre-fix-$TS
sudo install -d -m 0700 $B $B/lib $B/units $B/etc $B/state
sudo cp -a /usr/local/lib/homelab-backup $B/lib/
sudo cp -a /usr/local/sbin/backupctl $B/
sudo cp -a /etc/systemd/system/homelab-backup* $B/units/
# 設定與狀態（內含密碼檔，只存在 root 目錄）
sudo cp -a /etc/homelab-backup $B/etc/
sudo cp -a /var/lib/homelab-backup/state $B/state/
sudo ls -laR $B | head -40
echo "$TS"   # 記下來，回滾時要用
```

停用排程，避免安裝或手動驗證途中被 timer 觸發；**驗證通過前都不要重新啟用**
（見步驟 7）：

```bash
sudo systemctl disable --now homelab-backup.timer homelab-backup-maintenance.timer
```

## 2.（建議）設定告警通知

二選一：ntfy topic URL（例如 `https://ntfy.sh/<很長的隨機 topic>`），或
healthchecks.io ping URL（`https://hc-ping.com/<uuid>`；路徑含 `/ping/` 的自架
healthchecks 也會被當成 healthchecks）。

```bash
sudo install -m 0600 -o root -g root /dev/null /etc/homelab-backup/notify-url
sudoedit /etc/homelab-backup/notify-url      # 只放一行 URL
```

URL 檔的限制（不符合時只會印 `WARNING` 並略過推送，journal 的 `ALERT:` 照常）：

- 必須是 root 擁有的一般檔案（不跟隨 symlink），所在目錄也要受保護；
- mode 不可有任何 group/other 位元（0600 或更嚴格），例如 0640、0644 都會被拒絕；
- 只取第一個非空行，且必須是 `https://` URL。

在 `/etc/homelab-backup/config.yaml` 末尾加上（除 `notify_url_file` 外都可省略）：

```yaml
alert:
  notify_url_file: /etc/homelab-backup/notify-url
  # max_success_age: 36h   # 見下方說明
  # max_lock_age: 13h      # 預設 13h
  # renotify_after: 24h    # 預設 24h
```

- `max_success_age`：每個排程中的服務的門檻是「兩個排程週期 + 1 小時」
  （每日排程 = 49h）與此值兩者取**較大**者，所以這個值只能放寬、不能讓每週排程的
  服務在兩次執行之間誤報。`schedule.enabled: false` 的服務不檢查；從未成功過的服務
  從第一次被看到的時間起算。
- `max_lock_age`：全域鎖被同一個 backup／check 持有超過此值就告警。請保持大於
  `homelab-backup.service` 的 `TimeoutStartSec=12h`。
- `renotify_after`：同一個問題沒有變化時，至少隔這麼久才再推送一次。

> **注意：通知內容會離開主機。** `homelab-backup-notify@` 推送時會附上失敗 unit
> 最後 30 行 journal（`journalctl --unit <unit> --lines 30 --output cat`，最多約
> 3500 字元），可能包含服務名稱、路徑、容器名稱與錯誤訊息。ntfy 請用私人或自架
> server、夠長的隨機 topic（公開 ntfy.sh 上知道 topic 的人都能讀），或改用你信任的
> healthchecks 服務。

沒有設定 URL 也可以：失敗時 journal 裡一定會有 `ALERT:` 行，`systemctl --failed`
也看得到。

## 3. 處理已不存在或停止的容器

如果服務只是暫停，在該服務的 `backup.yaml` 中，會碰到該容器的 **backup** action
加上 `skip_if_not_running`（容器名稱或名稱清單）：

```yaml
actions:
  backup:
    before:
      - name: save-world
        command: [/usr/bin/docker, exec, -i, <container>, <save-command>]
        skip_if_not_running: <container>
        ...
```

行為：

- 執行前用 `docker inspect` 檢查；只有 Docker 明確回報容器**不存在**或**未執行**時
  才略過該 action，印出 `WARNING: skipping ...`，備份照常進行，snapshot 標記為
  best-effort。
- 無法判斷狀態（Docker daemon 無回應、找不到 docker 指令等）時**仍會執行** action
  並警告，不會把真正的失敗藏起來。
- 只支援 `actions.backup`；放在 `actions.restore` 會被 validate 拒絕
  （還原不可默默略過步驟）。

如果服務已經永久下線，改成 `schedule.enabled: false`，或把那個 stack 的
`backup.yaml` 移走。其他容器目前不在的服務，修好 helper 後請在步驟 6 確認它們的
備份結果。

## 4. 安裝新版

```bash
cd <repo-dir>
git status --short          # 應該是乾淨的
git log --oneline -8        # 確認在 fix/stop-the-bleeding
sudo ./install.sh
```

`install.sh` 會跑 `apt-get update/install`、建新的 venv 與 helper image、把
`Dockerfile.volume-rsync` 一起放進 release（之後 helper 被清掉時可自動重建）、
發布新的 launcher 和 systemd unit（含 `daemon-reload`），並把舊 release 保留成
`previous`。失敗時會自動回復。

## 5. 確認 unit（timer 保持停用）

```bash
sudo systemctl daemon-reload
systemd-analyze verify homelab-backup.service homelab-backup-maintenance.service homelab-backup-check.service homelab-backup-status.service homelab-backup-notify@test.service homelab-backup.timer homelab-backup-maintenance.timer homelab-backup-check.timer homelab-backup-status.timer
systemctl cat homelab-backup-maintenance.service | grep -c ExecStopPost   # 應為 0
systemctl list-timers --all 'homelab-backup*'                              # 都不應排定
```

新版 unit 一覽（皆為 `Type=oneshot`；逾時視為失敗，同樣會觸發 `OnFailure=`）：

| unit | 執行內容 | `TimeoutStartSec` | timer |
|---|---|---|---|
| `homelab-backup.service` | `backupctl run-due` | 12h | 每 5 分鐘 |
| `homelab-backup-maintenance.service` | `backupctl maintenance`（forget + prune） | 12h | 每週日 05:30 |
| `homelab-backup-check.service` | `backupctl check`（restic check） | 12h | 每週三 05:30 |
| `homelab-backup-status.service` | `backupctl status --notify` | 15min | 每 6 小時（xx:15） |
| `homelab-backup-notify@.service` | `backupctl notify-failure %i` | 5min | 由 `OnFailure=` 觸發 |

## 6. 手動驗證（依序，timer 仍停用）

```bash
backupctl --version
sudo backupctl validate                 # helper 不在時只會 WARNING，之後會自動重建
sudo backupctl status; echo "exit=$?"   # 仍有過期服務時應為 exit=2，並列出 ALERT
sudo backupctl unlock                   # 不帶 --remove-all，只清掉過期 lock
sudo backupctl backup <service>         # 先挑一個小服務
sudo backupctl snapshots <service>      # 應看到剛剛的 snapshot
sudo systemctl start homelab-backup.service   # 讓 run-due 跑其餘到期服務（可能很久）
journalctl -u homelab-backup.service -n 200 --no-pager
sudo backupctl status; echo "exit=$?"   # 全部成功後應為 exit=0
```

`status` 的行為：

- 不取得全域鎖，只讀 state 檔，所以 backup／check 執行中（或卡住）時也能跑。
- 每個服務輸出一行 JSON（含 `stale`、`last_success_age_seconds`、
  `max_success_age_seconds`），過期的服務另外印 `ALERT: <service>: no successful backup ...`。
- 若有其他程序持有全域鎖，會印出 `lock_holder`（pid、開始時間、已持有秒數）；
  持有超過 `alert.max_lock_age`（預設 13h）時印
  `ALERT: global lock held by ...; backup or check may be stalled`。
- 有過期服務或鎖卡住時 exit 2；`--max-age <duration>` 可暫時覆寫門檻。

`run-due` 的 retry 間隔是 30 分鐘；如果剛失敗過，`status` 會顯示 `retry at ...`，
可直接用 `sudo backupctl backup <service>` 手動觸發。

restic check 想立即跑一次：`sudo systemctl start homelab-backup-check.service`
（會等 backup 的 flock，可能要一段時間）。maintenance（forget + prune）照每週
排程跑即可，不需要現在手動跑。

## 7. 確認告警真的會響，然後才啟用 timer

```bash
# a) 通知管道本身（不用等到真的失敗）
sudo systemctl start homelab-backup-notify@manual-test.service
journalctl -u homelab-backup-notify@manual-test.service -n 20 --no-pager
#    應看到 "ALERT: systemd unit failed: manual-test"，並收到 ntfy／healthchecks 變紅

# b) 太久沒成功的門檻
sudo backupctl status --max-age 1m; echo "exit=$?"      # 應為 exit=2
sudo backupctl status --max-age 1m --notify              # 應收到「backups are stale」

# c) OnFailure= 串接（在仍有過期服務時執行）
sudo systemctl start homelab-backup-status.service
systemctl status 'homelab-backup-notify@homelab-backup-status.service.service' --no-pager
```

推送是節流的，測試時要知道：

- 已送出的告警記在 `<state_root>/_alerts.json`（預設
  `/var/lib/homelab-backup/state/_alerts.json`）。同一個 unit 的失敗、或內容相同的
  `status` 問題（相同的過期服務集合／鎖卡住／status 本身出錯），在
  `alert.renotify_after`（預設 24h）內不會再推送，只在 journal 記錄；問題內容改變時
  立即推送。測試想重複觸發時可刪掉這個檔案。
- `homelab-backup-status.service` 失敗時，若 `status --notify` 剛推送過同一個問題，
  `notify@` 只記 `ALERT:` 行而不再重複推送；所以 c) 可能只在 journal 看到。
- `status --notify` 在一切正常時：healthchecks 每次都送成功 ping（dead-man's switch）；
  ntfy 不送平常的 OK，只在先前推送過的問題解除時送一則 `recovered`。
- healthchecks 的失敗一律送到 `<url>/fail`；backup／maintenance／check 失敗後，
  下一次 status 正常時的成功 ping 會把 check 轉回綠色。請把 healthchecks 的
  period 設為 6 小時（status timer 週期），grace 至少留 1 小時。

全部通過後才啟用排程：

```bash
sudo systemctl enable --now homelab-backup.timer
sudo systemctl enable --now homelab-backup-maintenance.timer
sudo systemctl enable --now homelab-backup-check.timer
sudo systemctl enable --now homelab-backup-status.timer
systemctl list-timers 'homelab-backup*'
```

## 8. 回滾

**第一步：移除舊版不認得的新設定欄位。** 舊版會對未知欄位報
`unsupported fields` 而整個失敗，所以要先：

```bash
sudoedit /etc/homelab-backup/config.yaml   # 刪掉整個 alert: 區段
#   （或從 $B/etc/homelab-backup/config.yaml 還原；$TS 是步驟 1 記下的時間戳）
sudo grep -rln 'skip_if_not_running' /srv/stacks   # 依 services_root 調整路徑
#   逐一從列出的 backup.yaml 刪掉每個 skip_if_not_running 鍵
```

接著停用新的 timer：

```bash
sudo systemctl disable --now homelab-backup-check.timer homelab-backup-status.timer
sudo systemctl disable --now homelab-backup.timer homelab-backup-maintenance.timer
```

方案 A（建議）：從這個分支之前的最後一個 commit `c66cb2e` 重新安裝舊版。舊 release
的 helper image 可能已經被清掉，單純切回 symlink 一樣會失敗，重新安裝才會重建 helper：

```bash
git -C <repo-dir> switch --detach c66cb2e
sudo <repo-dir>/install.sh
sudo rm -f /etc/systemd/system/homelab-backup-check.* \
  /etc/systemd/system/homelab-backup-status.* \
  '/etc/systemd/system/homelab-backup-notify@.service'
sudo systemctl daemon-reload
sudo systemctl enable --now homelab-backup.timer homelab-backup-maintenance.timer
```

方案 B：從步驟 1 的備份還原檔案：

```bash
B=/root/homelab-backup-pre-fix-$TS
sudo cp -a $B/backupctl /usr/local/sbin/backupctl
sudo rm -f /etc/systemd/system/homelab-backup-check.* \
  /etc/systemd/system/homelab-backup-status.* \
  '/etc/systemd/system/homelab-backup-notify@.service'
sudo cp -a $B/units/. /etc/systemd/system/
sudo ln -sfn "$(sudo readlink $B/lib/homelab-backup/current)" /usr/local/lib/homelab-backup/current
sudo systemctl daemon-reload
# 舊 release 的 helper image 若已不存在，需手動重建（<helper-tag> 見
# /usr/local/lib/homelab-backup/current/volume-helper-image 的 "tag"）：
sudo docker build -t <helper-tag> -f <repo-dir>/Dockerfile.volume-rsync <repo-dir>
```

注意：舊 launcher 用 image ID 釘死 helper，重建出來的 image ID 不同，所以方案 B
仍無法讓 Docker volume 服務備份成功；只適合「新版本身有問題、先回到原狀」的情況。

## 9. 後續

- 找出是誰清掉了 helper image（`docker image prune -a`、`docker system prune -a`
  的 cron 或腳本）。新版會自動重建，但重建需要能拉 `Dockerfile.volume-rsync`
  指定的 `alpine` base image。
- 檢查 `skip_if_not_running` 標記的服務：snapshot 是 best-effort，長期停用的服務
  應改成 `schedule.enabled: false` 或移除 manifest。
- 確認 ntfy topic／healthchecks check 只有自己能讀（通知內含 journal 片段）。
