<template>
  <section id="consistency">
    <h2>11. 一致性模式</h2>
    <table>
      <thead><tr><th>模式</th><th>行為</th><th>適用情境</th></tr></thead>
      <tbody>
        <tr><td><code>stop</code></td><td>停止原本運行的 Compose services，完成備份後重啟。</td><td>持續寫入檔案、raw database volume。</td></tr>
        <tr><td><code>hooks</code></td><td>before hook 產生一致資料，備份後執行 after hook。</td><td>SQL dump、RCON save、應用快照。</td></tr>
        <tr><td><code>external</code></td><td>不控制服務，要求 writer 已由外部停止。</td><td>外部編排器已靜止的服務、唯讀資料。</td></tr>
        <tr><td><code>live</code></td><td>直接同步；偵測到 writer 時標成 <code>best-effort</code>。</td><td>可接受 fuzzy copy 的可重建內容。</td></tr>
        <tr><td><code>snapshot</code></td><td>對 Btrfs subvolume 建唯讀 snapshot，其餘來源直接同步。</td><td>需要低停機時間的檔案型服務。</td></tr>
      </tbody>
    </table>
    <div class="box warn">不確定時先使用 <code>stop</code>。它最保守，也最容易實際驗證還原結果。</div>
    <h3>Stop 與 hooks</h3>
    <pre v-pre><code>consistency:
  mode: stop
  timeout: 180

# 或
consistency:
  mode: hooks
  before:
    - ./scripts/dump-database.sh
  after: []</code></pre>
    <h3>備份交易 actions</h3>
    <p>
      <code>actions.before</code> 與 <code>actions.finally</code> 適用所有模式；
      <code>on_success</code> 在快照提交或未變略過後執行，
      <code>on_failure</code> 則在 staging、Restic、cleanup 或 state 失敗時執行。
      命令以 argv 執行，不經 shell。
    </p>
    <pre v-pre><code>actions:
  before:
    - name: save-palworld
      command:
        - /usr/bin/docker
        - compose
        - exec
        - -T
        - palworld
        - rcon-cli
        - Save
      timeout: 30
      required: true
      run_as: root
  finally:
    - name: notify-backup-complete
      command: [/usr/local/bin/notify-backup, complete]
      timeout: 30
      required: false
      run_as: root
  on_success:
    - name: notify-success
      command: [/usr/local/bin/notify-backup, success]
      timeout: 15
      required: false
      run_as: root
  on_failure:
    - name: notify-failure
      command: [/usr/local/bin/notify-backup-failure]
      timeout: 15
      required: false
      run_as: root</code></pre>
    <p>
      每個 action 都要設定 <code>run_as</code>，可用本機帳號或 <code>"UID:GID"</code>。
      相對 executable 會被拒絕；root action 的程式及父目錄必須由 root 擁有且不可由他人寫入。
    </p>
    <div class="box warn">
      Restic 成功後，required on-success action 即使失敗也無法撤回已提交的 snapshot。
      密碼與 token 不要放入 argv 或 manifest，應使用 root-only 設定或 secret。
    </div>
    <p>
      <code>snapshot</code> 的暫存 subvolume 會放在 trusted root 的 root-only workspace，
      並由 journal 追蹤與回收。多個 subvolume 依序建立，不保證跨 subvolume 原子性。
    </p>
  </section>

  <section id="sources">
    <h2>12. Bind mount 與 named volume</h2>
    <h3>Bind mount 與篩選規則</h3>
    <pre v-pre><code># compose.yaml
services:
  minecraft:
    volumes:
      - ./data:/data

# backup.yaml
sources:
  paths:
    - id: data
      path: ./data
      include:
        - world/**
        - config/**
        - mods/**
        - server.properties
      exclude:
        - mods/cache/**</code></pre>
    <p>
      <code>include</code> 是相對來源根目錄的 rsync 白名單，只適用於目錄；
      <code>exclude</code> 優先。空間預檢與原地還原使用相同規則，避免覆蓋未納入備份的現場資料。
    </p>
    <h3>路徑來源安全邊界</h3>
    <ul>
      <li>每個 path 必須嚴格位於唯一 trusted root 之下；控制路徑由 root 擁有且不可 group/world write。</li>
      <li>程式以固定 descriptor 逐層開啟來源與目標；中間 symlink 會被拒絕。</li>
      <li>來源本身是 symlink 時只保存 link 與目標字串，不跟隨 referent。</li>
      <li>支援 ext4、xfs、Btrfs；不支援 ZFS、FUSE、NFS、autofs 或 cross-mount。</li>
      <li>保存普通檔案、目錄、symlink、ACL、xattr 與 hardlink；拒絕 FIFO、socket 與 device node。</li>
      <li>inventory 保存數字 UID/GID 與 mode，讓全新 rebuild 能重建正確的資料祖先。</li>
    </ul>
    <h3>Compose named volume</h3>
    <pre v-pre><code># compose.yaml
services:
  db:
    volumes:
      - db-data:/var/lib/mysql
volumes:
  db-data:

# backup.yaml
sources:
  volumes:
    - id: database
      compose_volume: db-data</code></pre>
    <p><code>compose_volume</code> 是 Compose 頂層邏輯 key，工具會透過 <code>docker compose config --format json</code> 解析實際名稱。</p>
    <h3>外部 named volume</h3>
    <pre v-pre><code>sources:
  volumes:
    - id: shared-data
      name: company_shared_data</code></pre>
    <p><code>name</code> 只接受 Docker 管理的 volume 名稱，不接受 host path 或 mount option。</p>
    <h3>Anonymous volume</h3>
    <p><code>/data</code> 這類 anonymous volume 沒有穩定名稱，應改成 Compose named volume 才適合宣告式備份。</p>
  </section>

  <section id="cron">
    <h2>13. Cron 排程</h2>
    <p>格式固定為標準五欄：<code>分鐘 小時 日期 月份 星期</code>。</p>
    <table>
      <thead><tr><th>表達式</th><th>意義</th></tr></thead>
      <tbody>
        <tr><td><code>0 2 * * *</code></td><td>每天 02:00</td></tr>
        <tr><td><code>0 2,8,14,20 * * *</code></td><td>每天 02:00、08:00、14:00、20:00</td></tr>
        <tr><td><code>*/15 * * * *</code></td><td>每 15 分鐘</td></tr>
        <tr><td><code>0 */6 * * *</code></td><td>每 6 小時整點</td></tr>
        <tr><td><code>30 3 * * mon-fri</code></td><td>週一到週五 03:30</td></tr>
      </tbody>
    </table>
    <p>排程器每 5 分鐘掃描，實際開始可能晚數分鐘。執行期間經過的時點不排隊；完成後等待下一個有效時點。</p>
    <ul>
      <li>systemd oneshot 不會同時啟動第二份相同 service。</li>
      <li>全域 <code>flock</code> 阻止備份、還原與維護重疊。</li>
      <li>多個到期服務依序執行，不平行寫入 repository。</li>
    </ul>
    <h3>失敗重試</h3>
    <pre v-pre><code>schedule:
  cron: "0 */6 * * *"
  retry_after: 30m
  max_lateness: 2h</code></pre>
    <p>失敗後至少等 30 分鐘；若該時點已遲到超過 2 小時，放棄並等待下一時點。</p>
  </section>

  <section id="retention">
    <h2>14. 每個服務的保留策略</h2>
    <pre v-pre><code>retention:
  keep_last: 8
  keep_daily: 14
  keep_weekly: 8
  keep_monthly: 12
  keep_yearly: 3</code></pre>
    <table>
      <thead><tr><th>欄位</th><th>意義</th></tr></thead>
      <tbody>
        <tr><td><code>keep_last</code></td><td>保留最新 N 份。</td></tr>
        <tr><td><code>keep_hourly</code></td><td>最近 N 個有 snapshot 的小時，各留一份。</td></tr>
        <tr><td><code>keep_daily</code></td><td>最近 N 個有 snapshot 的日期，各留一份。</td></tr>
        <tr><td><code>keep_weekly</code></td><td>最近 N 個有 snapshot 的週，各留一份。</td></tr>
        <tr><td><code>keep_monthly</code></td><td>最近 N 個有 snapshot 的月份，各留一份。</td></tr>
        <tr><td><code>keep_yearly</code></td><td>最近 N 個有 snapshot 的年份，各留一份。</td></tr>
      </tbody>
    </table>
    <div class="box warn">
      多個條件採聯集，符合任一規則就保留。若要嚴格只留最新 30 份，只設定 <code>keep_last: 30</code>。
    </div>
  </section>

  <section id="examples">
    <h2>15. 服務範例</h2>
    <table>
      <thead><tr><th>檔案</th><th>重點</th></tr></thead>
      <tbody>
        <tr><td><code>examples/minecraft.backup.yaml</code></td><td>停止服務後備份 bind mount。</td></tr>
        <tr><td><code>examples/ghost.backup.yaml</code></td><td>同時備份 bind mount 與 MySQL named volume。</td></tr>
        <tr><td><code>examples/palworld.backup.yaml</code></td><td>以 Btrfs snapshot 與 action 實作低停機備份。</td></tr>
      </tbody>
    </table>
    <div class="box warn">資料庫建議用 <code>hooks</code> 產生 logical dump；若備份 raw volume，還原環境應使用相同產品與 major version。</div>
  </section>
</template>
