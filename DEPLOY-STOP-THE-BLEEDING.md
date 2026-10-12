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
- `renotify_after`：同一個問題沒有變化時，至少隔這麼久才再推送一次（run-due 的
  失敗是每個服務各自計算，見步驟 7）。

> **注意：通知內容會離開主機。** 通知內文會列出每個失敗服務（run-due）或每個
> status 錯誤／過期服務的 `last error` 的**第一行**（截斷為 200 字元）；
> `homelab-backup-notify@` 推送時另外附上失敗 unit 最後 30 行 journal
> （`journalctl --unit <unit> --lines 30 --output cat`）。每則推送（`notify@` 與
> `status --notify` 皆同）標題加內文以 UTF-8 計最多 3900 bytes（ntfy 單則上限
> 4096 bytes）：摘要放最前面，放不下時從 journal 開頭裁掉、只保留結尾（摘要本身
> 過長時截斷摘要結尾、不附 journal；`status --notify` 沒有 journal，只會截斷摘要），
> 一律在字元邊界切開，不會產生不合法的 UTF-8；被裁掉處以 `…` 標示。
> 這些內容可能包含服務名稱、路徑、容器名稱與錯誤訊息。ntfy 請用私人或自架
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
sudo ./target/release/backupctl install
```

`backupctl install` 會跑 `apt-get update/install`、安裝 `backupctl` 二進位與 helper image、把
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

check 逾時的評估：`homelab-backup-check.service` 維持 `TimeoutStartSec=12h`。restic
check 只透過 rclone 讀取 `check.read_data_subset`（預設 5%）的資料，12h 相當充裕。
因為 check 執行期間持有全域鎖，這個值必須**小於** `alert.max_lock_age`（預設 13h），
否則正常的 check 也會被 `status` 當成鎖卡住而告警。若 check 確實需要更久，請同時
調高 `TimeoutStartSec` 與 `alert.max_lock_age`，或調低 `read_data_subset`。

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
  `/var/lib/homelab-backup/state/_alerts.json`）。同一個問題在
  `alert.renotify_after`（預設 24h）內不會再推送，只在 journal 記錄。測試想重複觸發
  時可刪掉這個檔案。
- 讀寫 `_alerts.json` 時都會持有 `<state_root>/_alerts.lock`（不是全域鎖，所以
  backup 卡住時告警照常運作），多個 notify／status 同時執行也不會互相覆蓋狀態。
- **`homelab-backup.service`（run-due）依服務分別節流。** run-due 結束時記下這一輪
  失敗的服務（含錯誤第一行）與本次的 `INVOCATION_ID`。`notify-failure` 只推送
  「剛進入失敗」（沒有紀錄）或上次推送已超過 `renotify_after` 的服務；同一則通知
  可列出多個服務，其他仍在失敗但已通知過的服務只在 `failed: ...` 行下方附註名稱
  （在各服務明細之前，內文過長被截斷時也不會被切掉）。某服務備份**成功**只清掉
  該服務的紀錄；這一輪沒排到的服務保留紀錄；「No backups are due」或其他服務成功
  都不會清掉別人的紀錄。所以一個服務持續失敗、其他服務照常成功時，24h 內只推送
  一次；修好後再壞會立即推送。節流只看服務名稱（每個服務只記 `notified_at`）：
  仍在失敗的服務即使錯誤訊息變了，在 `renotify_after` 內也不會再推送，新的錯誤
  只出現在 journal 與 `status` 的 `last_error`。已從 manifest 移除的服務，其紀錄
  只會在有嘗試備份或有服務失敗的一輪被清掉（「No backups are due」的一輪不會），
  所以清理會延後到下一次有備份排到的一輪。
- **run-due 沒做完紀錄就結束**（crash、逾時、OOM、被 kill、設定錯誤）時，
  `notify-failure` 不採用舊的失敗紀錄：只有紀錄裡的 invocation id 等於 systemd
  傳來的 `MONITOR_INVOCATION_ID` 才採用。否則以獨立問題推送，標題如
  `homelab-backup.service failed (timeout)`，內文附 `MONITOR_SERVICE_RESULT`／
  `MONITOR_EXIT_CODE`／`MONITOR_EXIT_STATUS`。這類推送依結果類型（`crash:<result>`）
  分別節流：第一次或換成新的結果類型時立即推送，持續發生時每 `renotify_after`
  最多一次（沒推送成功的那次不算）。每次 crash（不論有沒有推送）都會更新該類型的
  `last_seen_at`，並把當下備份狀態（`last_result`）仍是 `running` 的服務名稱
  併入該紀錄的 `running` 清單（同一類型重複 crash 時取聯集；讀不到或損壞的備份
  狀態略過，manifest 讀不到時清單為空）。任何一輪做完紀錄的 run-due——包括
  「No backups are due」的一輪與有服務失敗的一輪——都會檢查 `crash:<result>` 紀錄：
  - 有 `running` 清單時：清單中的服務在之後某一輪備份**成功**、或已從 manifest
    移除，就從清單拿掉；`last_seen_at` 已滿 `renotify_after` 時整筆清掉。清單
    拿空後改用下一條規則。
  - 沒有（或已拿空）`running` 清單時（crash 不在備份途中，或舊版寫的紀錄）：
    `last_seen_at` 已滿 1 小時就清掉。
  （「No backups are due」的一輪不動各服務的紀錄，沒有要改的 crash 紀錄時也不會
  建立或改寫 `_alerts.json`。）crash 的那一輪本身沒做完紀錄，不會清任何東西。
  所以「每小時（或每 30 分鐘）被 OOM kill 一次、中間各輪正常」24h 內只推送一次；
  在某服務備份途中 crash、之後該服務一直沒有成功備份（例如每天同一個服務都被
  OOM kill，中間只有「No backups are due」，或該服務一直在失敗）時，紀錄保留到
  安靜滿 `renotify_after`，所以同一種 crash 每 `renotify_after` 最多推送一次
  （預設 24h：每天被 kill 一次的每日服務 72h 內推送 3 次，每 2h 或 6h 被 kill
  一次則 24h 內只推送 1 次）。被 kill 的服務之後成功備份過、且 crash 停了至少
  1 小時後再發生，或 crash 停了 `renotify_after` 後再發生，都會立即推送。
  不在備份途中的 crash（例如設定錯誤）停了至少 1 小時（期間至少有一輪正常結束）
  後再發生會立即推送，即使只有每日排程的服務、或有服務一直在失敗也一樣。
  systemd 低於 251（沒有 `MONITOR_INVOCATION_ID`）時，
  改採 20 分鐘內、尚未被前一次 notify-failure 用過的紀錄。
- maintenance／check 依 unit 節流；成功跑完一次會清掉該 unit 的紀錄，下次再失敗
  立即推送。`status` 跑完且一切正常（沒有過期、沒有鎖卡住、沒有 status 錯誤）時
  也會清掉 `homelab-backup-status.service` 的紀錄。
- `status` 問題內容（過期服務集合／鎖卡住／status 本身出錯）改變時立即推送。
- `homelab-backup-status.service` 失敗時，若 `status --notify` 剛推送過同一個問題
  （exit 2），`notify@` 只記 `ALERT:` 行而不再重複推送；所以 c) 可能只在 journal
  看到。判斷依據是 status 記下的 `INVOCATION_ID` 必須等於 `MONITOR_INVOCATION_ID`；
  有 `MONITOR_INVOCATION_ID` 但紀錄沒有 id 時一律推送，只有 systemd 沒提供
  `MONITOR_INVOCATION_ID` 時才退回 20 分鐘時間窗。`status` 本身出錯（exit 1）則一律
  由 `notify-failure` 推送，所以這時會收到**兩則**通知：`status --notify` 自己的
  推送（`status error: ...`，依問題內容節流）與 `OnFailure=` 觸發的
  `homelab-backup-status.service failed`（依 unit 與 `renotify_after` 節流）。
- `run-due` 拿不到全域鎖時，會先以 100 ms 間隔重試 3 次，仍拿不到
  才印 `SKIP:` 跳過這一輪（避免剛好撞上前一輪收尾而白白錯過 5 分鐘）。
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

順序很重要：**先停排程、等執行中的工作結束，再改設定**。否則每 5 分鐘的 timer 可能
在設定改到一半時觸發，或讓新版程式讀到已經改成舊格式的設定。

**第一步：停用全部四個 timer。**

```bash
sudo systemctl disable --now homelab-backup.timer homelab-backup-maintenance.timer \
  homelab-backup-check.timer homelab-backup-status.timer
```

**第二步：等執行中的 unit 結束。** `disable --now` 只停 timer，不會中斷已經在跑的
service；**不要**強制停止執行中的備份，等它自己跑完。這些都是 `Type=oneshot`，
執行中顯示為 `activating`（不是 `active`），所以不能只看 `is-active --quiet` 的
exit code。除了 backup／maintenance／check，也要等 `homelab-backup-status.service`
與所有 `homelab-backup-notify@*.service` instance（它們會讀寫 `_alerts.json`，也會
讀設定）。`systemctl is-active` 不會替沒載入的 unit 展開萬用字元，所以改用
`list-units`，只要它還列出任何東西就繼續等：

```bash
systemctl list-jobs 'homelab-backup*'              # 執行中的 oneshot 會列出 start job
while [ -n "$(systemctl list-units --type=service --state=activating,active \
    'homelab-backup*' --no-legend)" ]; do
  sleep 30
done
systemctl list-units --type=service --all 'homelab-backup*' --no-legend
#   所有 homelab-backup*.service（含 status 與 notify@*）都應為 inactive 或 failed
```

**第三步：移除舊版不認得的新設定欄位。** 舊版會對未知欄位報
`unsupported fields` 而整個失敗，所以要先：

```bash
sudoedit /etc/homelab-backup/config.yaml   # 刪掉整個 alert: 區段
#   （或從 $B/etc/homelab-backup/config.yaml 還原；$TS 是步驟 1 記下的時間戳）
sudo grep -rln 'skip_if_not_running' /srv/stacks   # 依 services_root 調整路徑
#   逐一從列出的 backup.yaml 刪掉每個 skip_if_not_running 鍵
```

**第四步：選擇方案 A 或方案 B。** 兩者最後都只重新啟用舊版原有的兩個 timer。

方案 A（建議）：從這個分支之前的最後一個 commit `c66cb2e` 重新安裝舊版。舊 release
的 helper image 可能已經被清掉，單純切回 symlink 一樣會失敗，重新安裝才會重建 helper：

```bash
git -C <repo-dir> switch --detach c66cb2e
sudo <repo-dir>/target/release/backupctl install
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
sudo systemctl enable --now homelab-backup.timer homelab-backup-maintenance.timer
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
