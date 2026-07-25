<template>
  <section id="validate">
    <h2>16. 驗證並初始化 repository</h2>
    <pre v-pre><code>sudo backupctl list
sudo backupctl validate</code></pre>
    <p>驗證內容包括：</p>
    <ul>
      <li>全域 YAML、所有 manifest 與全域唯一 service ID。</li>
      <li>控制目錄 ownership、權限、symlink 與 filesystem 邊界。</li>
      <li>Compose 設定、required paths 與 Docker volumes。</li>
      <li>Cron、retention、consistency、helper image、Restic、rclone 與 rsync。</li>
    </ul>
    <h3>第一次建立 repository</h3>
    <pre v-pre><code>sudo backupctl init</code></pre>
    <div class="box warn"><code>init</code> 只在 repository 尚不存在時執行；重建主機並連回既有 repository 時不要執行。</div>
  </section>

  <section id="first-backup">
    <h2>17. 執行首次備份</h2>
    <pre v-pre><code>sudo backupctl backup advent-of-ascension-plus-2026
sudo backupctl snapshots advent-of-ascension-plus-2026</code></pre>
    <p>
      指定服務時，snapshots 也會依目前 manifest 顯示唯讀 retention preview 與保留原因；
      不會執行 forget 或 prune。
    </p>
    <h3>刪除指定 snapshot</h3>
    <pre v-pre><code>sudo backupctl delete-snapshot advent-of-ascension-plus-2026 01234567
sudo backupctl delete-snapshot advent-of-ascension-plus-2026 01234567 --prune --yes</code></pre>
    <h3>備份全部啟用服務</h3>
    <pre v-pre><code>sudo backupctl backup</code></pre>
    <p>批次工作會隔離單一服務錯誤；只有所有來源 staging 完成後才提交 snapshot，最後統一回報失敗摘要。</p>
    <p>空間不足時預設中止；只有人工操作可加上：</p>
    <pre v-pre><code>sudo backupctl backup advent-of-ascension-plus-2026 --allow-low-space
sudo backupctl status</code></pre>
    <div class="box warn">人工放行不代表複製一定成功；遇到 <code>ENOSPC</code> 仍會失敗並清除半成品 staging。</div>
  </section>

  <section id="restore-test">
    <h2>18. 還原、選取與一次還原全部服務</h2>
    <p>不指定 ID 時會列出 repository 內的服務並開啟互動選單，所有項目預設勾選：</p>
    <pre v-pre><code>sudo backupctl restore</code></pre>
    <table>
      <thead><tr><th>按鍵</th><th>功能</th></tr></thead>
      <tbody>
        <tr><td>↑／↓</td><td>移動游標</td></tr>
        <tr><td>Space</td><td>切換目前服務</td></tr>
        <tr><td>A</td><td>全選或全部取消</td></tr>
        <tr><td>Enter</td><td>確認</td></tr>
        <tr><td>Q／Esc</td><td>取消</td></tr>
      </tbody>
    </table>
    <h3>批次與指定 snapshot</h3>
    <pre v-pre><code>sudo backupctl restore --all
sudo backupctl restore --all --apply --start --yes
sudo backupctl restore minecraft ghost --apply
sudo backupctl restore minecraft --snapshot 01234567 --apply</code></pre>
    <p>非 TTY 的所有 restore 都必須明確加入 <code>--yes</code>；空間不足則需另加 <code>--allow-low-space</code>。</p>
    <h3><code>backup.yaml</code> 還原規則</h3>
    <ul>
      <li><strong>現有部署：</strong>本機 manifest、Compose files 與 snapshot inventory 必須相符。</li>
      <li><strong>全新重建：</strong>相關設定、paths、volumes 與 container references 必須全部不存在。</li>
      <li><strong>混合狀態：</strong>只有部分 target 存在時一律拒絕，不猜測身分或覆寫。</li>
      <li><code>--restore-manifest</code> 只允許安全投影一致的 manifest；<code>--keep-manifest</code> 保留本機版本。</li>
    </ul>
    <p>只下載時資料保留在 <code>/var/lib/homelab-backup/restores/&lt;service&gt;/&lt;timestamp&gt;</code>；可清理：</p>
    <pre v-pre><code>sudo backupctl cleanup-restores minecraft/20260717-120000-000000001 --yes
sudo backupctl cleanup-restores --all --yes</code></pre>
    <div class="box warn">上線前至少實際執行一次 <code>restore --apply</code>；成功上傳不等於能成功還原。</div>
  </section>

  <section id="enable">
    <h2>19. 啟用自動排程</h2>
    <p>先確認 validate、每個服務首次備份、實際還原測試與主機外的加密設定備援均已完成。</p>
    <pre v-pre><code>sudo systemctl enable --now homelab-backup.timer
sudo systemctl enable --now homelab-backup-maintenance.timer

systemctl list-timers 'homelab-backup*'
systemctl status homelab-backup.timer</code></pre>
    <p>每 5 分鐘 timer 執行 <code>backupctl run-due</code>，只備份當下到期的服務。</p>
  </section>

  <section id="monitor">
    <h2>20. 監控與 repository 維護</h2>
    <pre v-pre><code># 服務紀錄
journalctl -u homelab-backup.service -n 200 --no-pager
journalctl -u homelab-backup.service -f

# 狀態與快照
sudo backupctl status
sudo backupctl snapshots

# 預覽與正式維護
sudo backupctl maintenance --dry-run
sudo backupctl maintenance
sudo backupctl check</code></pre>
    <h3>非正常中斷後移除 stale lock</h3>
    <pre v-pre><code>sudo backupctl unlock</code></pre>
    <p>只有確認沒有其他 Restic process 執行時才能使用；本機 <code>flock</code> 會隨 process 自動釋放。</p>
  </section>

  <section id="secrets">
    <h2>21. 災難復原必要材料</h2>
    <table>
      <thead><tr><th>材料</th><th>用途</th></tr></thead>
      <tbody>
        <tr><td>本專案程式碼</td><td>重新 clone 安裝。</td></tr>
        <tr><td><code>homelab-backup-configs.zip.age</code></td><td>加密保存 Restic 密碼、rclone 與全域設定。</td></tr>
        <tr><td>對應 SSH 私鑰</td><td>解密設定 archive；保存在 Bitwarden 或離線媒體。</td></tr>
        <tr><td>OneDrive Restic repository</td><td>保存服務資料、快照、Compose、manifest 與目錄 metadata。</td></tr>
      </tbody>
    </table>
    <div class="box ok">不必另外備份整個 <code>/srv/stacks</code>；private Git 只是額外版本管理。</div>
    <div class="box warn">遺失 Restic 密碼後，即使 repository 還在也無法解密。</div>
  </section>

  <section id="disaster">
    <h2>22. 主機全毀後的最短重建流程</h2>
    <h3>1. 重新取得專案並安裝</h3>
    <pre v-pre><code>cd ~/Apps
git clone https://github.com/flier268/homelab-backup.git
cd homelab-backup
sudo ./install.sh</code></pre>
    <h3>2. 放回必要設定</h3>
    <p>準備加密設定 archive 與 Bitwarden 中的 SSH 私鑰，再執行：</p>
    <pre v-pre><code>sudo ./restore-configs.sh</code></pre>
    <h3>3. 測試 OneDrive 與 repository</h3>
    <pre v-pre><code>sudo rclone lsd onedrive: \
  --config /etc/homelab-backup/rclone/rclone.conf
sudo backupctl snapshots</code></pre>
    <div class="box warn">這是連回既有 repository，<strong>不要執行 <code>backupctl init</code></strong>。</div>
    <h3>4. 還原全部服務</h3>
    <pre v-pre><code># 互動模式，預設全選
sudo backupctl restore --apply --start

# 完全非互動
sudo backupctl restore --all \
  --restore-manifest --apply --start --yes</code></pre>
    <h3>5. 檢查服務，最後才開排程</h3>
    <pre v-pre><code>docker ps
cd "/srv/stacks/Minecraft/Advent of Ascension Plus-2026"
docker compose ps
docker compose logs --tail=200

sudo systemctl enable --now homelab-backup.timer
sudo systemctl enable --now homelab-backup-maintenance.timer</code></pre>
  </section>
</template>
