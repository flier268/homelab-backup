# Encrypted disaster recovery configuration

`sudo ../backup-configs.sh` 的 Git 模式只會在此建立：

- `homelab-backup-configs.zip.age`

檔案內含 `restic-password`、`rclone.conf` 與 `config.yaml`，先封裝為 ZIP，再以
`age` 加密。三個明文設定不會寫入此目錄。

腳本會先確認 `/run` 是 tmpfs 才建立明文暫存檔。本專案沒有舊明文設定或舊
archive 的遷移流程；全域設定、服務 manifest 與 snapshot inventory 都使用目前
唯一的 `version: 1`，不處理升級或舊版 fallback。

功能邊界：只支援貼入 `ssh-ed25519` 或 `ssh-rsa` 公鑰；age 私鑰於還原或換 KEY
時從標準輸入直接交給 `age`，本專案不管理 SSH agent、金鑰生命週期或遠端
祕密管理服務。換 KEY 時 archive 必須由執行 `sudo` 的非 root 使用者擁有，
路徑的讀取與原子替換皆以該使用者權限執行。加密檔仍建議只放在受控的
private repository。
