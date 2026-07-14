# Testing

已執行下列靜態與 smoke tests：

- `python3 -m py_compile backupctl`
- `bash -n install.sh`
- `bash -n backup-configs.sh`
- `bash -n restore-configs.sh`
- `shellcheck install.sh backup-configs.sh restore-configs.sh`
- `python3 -m unittest discover -s tests -v`
- `backupctl --help`

自動化測試涵蓋 manifest schema 與路徑安全、Cron 解析、staging 清理、
hook／服務重啟、retention 狀態、snapshot host 範圍、restore preflight、
required／excluded sources、volume 防清空、manifest 原子替換，以及部署腳本安全性。

仍需在實際主機執行：

1. `sudo backupctl validate`
2. 建立測試 snapshot
3. 暫時移走一個服務的本機 `backup.yaml`
4. 執行 `sudo backupctl restore <service>`，確認 manifest 自動復原
5. 執行互動式 `sudo backupctl restore`，確認預設全選與鍵盤操作
6. 在隔離環境測試 `--apply --start`
7. 執行 `backup-configs.sh` 與 `restore-configs.sh` 的完整往返測試
