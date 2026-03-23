const express = require('express');
const { Pool } = require('pg');
const path = require('path');

const app = express();
const PORT = 3080;

const pool = new Pool({ connectionString: 'postgresql://step1ne@localhost:5432/step1ne' });

const CRAWLER_URL = 'http://localhost:5000';
const HR_URL = 'http://localhost:3003';
const CDP_URL = 'http://localhost:9222';
const API_KEY = 'PotfZ42-qPyY4uqSwqstpxllQB1alxVfjJsm3Mgp3HQ';

const headers = { 'Authorization': `Bearer ${API_KEY}`, 'Content-Type': 'application/json' };

async function safeFetch(url, opts = {}) {
  try {
    const res = await fetch(url, { ...opts, signal: AbortSignal.timeout(5000) });
    return res.ok ? await res.json() : null;
  } catch { return null; }
}

async function getServiceStatus() {
  const [hr, crawler, cdp] = await Promise.all([
    safeFetch(`${HR_URL}/api/health`),
    safeFetch(`${CRAWLER_URL}/api/health`),
    safeFetch(`${CDP_URL}/json/version`),
  ]);
  return { hr_api: !!hr, crawler: !!crawler, chrome_cdp: !!cdp };
}

async function getTodayTasks() {
  const data = await safeFetch(`${CRAWLER_URL}/api/tasks`, { headers });
  if (!data) return { tasks: [], today: [] };
  const tasks = Array.isArray(data) ? data : (data.tasks || data.data || []);
  const today = new Date().toISOString().slice(0, 10);
  const todayTasks = tasks.filter(t => (t.created_at || '').includes(today));
  return { tasks, today: todayTasks };
}

async function getCrawlerStats() {
  return await safeFetch(`${CRAWLER_URL}/api/dashboard/stats`, { headers });
}

async function getDBStats() {
  try {
    const today = new Date().toISOString().slice(0, 10);
    const [total, todayNew, withPdf, pipeline, grades, recentImports] = await Promise.all([
      pool.query('SELECT count(*) FROM candidates_pipeline'),
      pool.query('SELECT count(*) FROM candidates_pipeline WHERE created_at::date = $1', [today]),
      pool.query("SELECT count(*) FROM candidates_pipeline WHERE created_at::date = $1 AND resume_files IS NOT NULL AND resume_files::text NOT IN ('[]', 'null', '')", [today]),
      pool.query("SELECT status, count(*) FROM candidates_pipeline GROUP BY status ORDER BY count DESC"),
      pool.query("SELECT ai_grade, count(*) FROM candidates_pipeline WHERE created_at::date = $1 GROUP BY ai_grade", [today]),
      pool.query("SELECT id, name, current_position, current_company, ai_grade, recruiter, source, created_at FROM candidates_pipeline WHERE created_at::date = $1 ORDER BY created_at DESC LIMIT 20", [today]),
    ]);
    return {
      total: parseInt(total.rows[0].count),
      today_new: parseInt(todayNew.rows[0].count),
      today_with_pdf: parseInt(withPdf.rows[0].count),
      pipeline: pipeline.rows.reduce((acc, r) => { acc[r.status] = parseInt(r.count); return acc; }, {}),
      today_grades: grades.rows.reduce((acc, r) => { acc[r.ai_grade || '未評級'] = parseInt(r.count); return acc; }, {}),
      recent_imports: recentImports.rows,
    };
  } catch (e) { return { error: e.message }; }
}

async function getJobStats() {
  try {
    const [recruiterDist, activeJobs] = await Promise.all([
      pool.query("SELECT recruiter, count(*) FROM jobs_pipeline WHERE job_status = '招募中' GROUP BY recruiter ORDER BY count DESC"),
      pool.query("SELECT count(*) FROM jobs_pipeline WHERE job_status = '招募中'"),
    ]);
    return {
      active_jobs: parseInt(activeJobs.rows[0].count),
      by_recruiter: recruiterDist.rows.reduce((acc, r) => { acc[r.recruiter] = parseInt(r.count); return acc; }, {}),
    };
  } catch (e) { return { error: e.message }; }
}

async function getNotifications() {
  const data = await safeFetch(`${HR_URL}/api/notifications`, { headers });
  if (!data) return [];
  const notifs = Array.isArray(data) ? data : (data.notifications || data.data || []);
  return notifs.slice(0, 20);
}

function determinePhase(todayTasks, dbStats) {
  const total = todayTasks.length;
  const completed = todayTasks.filter(t => t.status === 'completed').length;
  const running = todayTasks.filter(t => t.status === 'running').length;
  const totalLinkedin = todayTasks.reduce((s, t) => s + (t.linkedin_count || 0), 0);
  const imported = dbStats.today_new || 0;
  const withPdf = dbStats.today_with_pdf || 0;

  const runningTask = todayTasks.find(t => t.status === 'running');

  if (total === 0) {
    return {
      current_phase: 0,
      phase_label: '等待閉環啟動',
      phases: buildPhases(0, {}),
      jobs_progress: { done: 0, total: 0 },
      current_task: null,
    };
  }

  let phase = 1;
  let detail = {};

  detail.search = `${totalLinkedin} 人`;
  detail.filter = `A層通過 ${imported} 人`;
  detail.import_detail = `${imported} 人已入庫`;

  if (running > 0) {
    phase = 1;
    detail.current = runningTask ? `${runningTask.job_title || ''} (${runningTask.progress || 0}%)` : '';
  } else if (completed === total && imported > 0 && withPdf === 0) {
    phase = 3;
  } else if (withPdf > 0 && withPdf < imported) {
    phase = 4;
    detail.pdf = `${withPdf}/${imported}`;
  } else if (withPdf > 0 && withPdf >= imported) {
    phase = 5;
  }

  const hasGrades = Object.keys(dbStats.today_grades || {}).some(g => g && g !== '未評級' && g !== 'null');
  if (phase >= 5 && hasGrades) phase = 6;

  return {
    current_phase: phase,
    phase_label: ['', '搜尋+篩選', '篩選', '匯入', 'PDF 下載', '重新評級', '群組通知'][phase] || '完成',
    phases: buildPhases(phase, detail),
    jobs_progress: { done: completed, total },
    current_task: runningTask ? {
      id: runningTask.id,
      job_id: runningTask.step1ne_job_id,
      title: runningTask.job_title,
      progress: runningTask.progress,
    } : null,
  };
}

function buildPhases(current, detail) {
  const names = ['搜尋', '篩選', '匯入', 'PDF下載', '評級', '通知'];
  return names.map((name, i) => {
    const phaseNum = i + 1;
    let status = 'waiting';
    if (phaseNum < current) status = 'done';
    else if (phaseNum === current) status = 'running';
    // phases 1-3 are bundled in the crawler
    if (current >= 3 && phaseNum <= 3) status = 'done';
    if (current >= 1 && current <= 2 && phaseNum <= current) status = 'running';

    let d = '';
    if (phaseNum === 1) d = detail.search || '';
    if (phaseNum === 2) d = detail.filter || '';
    if (phaseNum === 3) d = detail.import_detail || '';
    if (phaseNum === 4) d = detail.pdf || '';

    return { name, status, detail: d, current: phaseNum === current ? (detail.current || '') : '' };
  });
}

async function getPdfLogs() {
  try {
    const today = new Date().toISOString().slice(0, 10);
    const result = await pool.query(
      `SELECT id, name, current_position, current_company, updated_at
       FROM candidates_pipeline
       WHERE created_at::date = $1
         AND resume_files IS NOT NULL
         AND resume_files::text NOT IN ('[]', 'null', '')
       ORDER BY updated_at DESC LIMIT 30`, [today]
    );
    return result.rows.map(r => {
      const ts = r.updated_at instanceof Date ? r.updated_at.toISOString() : String(r.updated_at || '');
      return {
        time: ts.slice(11, 19),
        icon: '📄',
        message: `PDF 已上傳 — #${r.id} ${r.name} (${r.current_position || '?'} @ ${r.current_company || '?'})`,
      };
    });
  } catch { return []; }
}

async function getSystemLogs() {
  try {
    const data = await safeFetch(`${HR_URL}/api/system-logs`, { headers });
    if (!data) return [];
    const logs = Array.isArray(data) ? data : (data.logs || []);
    const today = new Date().toISOString().slice(0, 10);
    return logs
      .filter(l => (l.created_at || l.timestamp || '').includes(today))
      .slice(0, 20)
      .map(l => {
        const ts = String(l.created_at || l.timestamp || '');
        return {
          time: ts.slice(11, 19),
          icon: l.action?.includes('resume') ? '📄' : l.action?.includes('import') ? '📥' : '📝',
          message: `${l.action || ''} — ${l.details || l.message || ''}`.slice(0, 100),
        };
      });
  } catch { return []; }
}

async function buildLogs(todayTasks, recentImports) {
  const logs = [];

  for (const t of todayTasks.sort((a, b) => (b.last_run || '').localeCompare(a.last_run || ''))) {
    const time = (t.last_run || t.created_at || '').slice(11, 19);
    if (t.status === 'completed') {
      logs.push({ time, icon: '✅', message: `爬蟲完成 — ${t.job_title || '?'} | LinkedIn ${t.linkedin_count || 0} 人` });
    } else if (t.status === 'running') {
      logs.push({ time, icon: '🔄', message: `搜尋中 — ${t.job_title || '?'} (${t.progress || 0}%)` });
    }
  }

  for (const c of (recentImports || [])) {
    const ts = c.created_at instanceof Date ? c.created_at.toISOString() : String(c.created_at || '');
    const time = ts.slice(11, 19);
    logs.push({ time, icon: '📥', message: `匯入 #${c.id} ${c.name} — ${c.current_position || '?'} @ ${c.current_company || '?'}` });
  }

  // Add PDF download logs
  const pdfLogs = await getPdfLogs();
  logs.push(...pdfLogs);

  // Add system logs
  const sysLogs = await getSystemLogs();
  logs.push(...sysLogs);

  // Dedupe by time+message, sort newest first
  const seen = new Set();
  const unique = logs.filter(l => {
    const key = `${l.time}${l.message}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  unique.sort((a, b) => b.time.localeCompare(a.time));
  return unique.slice(0, 50);
}

app.use(express.static(path.join(__dirname, 'public')));

app.get('/api/status', async (req, res) => {
  const [services, { today: todayTasks }, dbStats, jobStats, notifications, crawlerStats] = await Promise.all([
    getServiceStatus(),
    getTodayTasks(),
    getDBStats(),
    getJobStats(),
    getNotifications(),
    getCrawlerStats(),
  ]);

  const consultantPhase = determinePhase(todayTasks, dbStats);
  const logs = await buildLogs(todayTasks, dbStats.recent_imports);

  res.json({
    timestamp: new Date().toISOString(),
    services,
    consultant_lobster: {
      ...consultantPhase,
      logs,
    },
    ceo_lobster: {
      current_phase: 0,
      phase_label: '等待 heartbeat',
      phases: [
        { name: '健康檢查', status: 'waiting' },
        { name: '讀回報', status: 'waiting' },
        { name: 'Pipeline掃描', status: 'waiting' },
        { name: '品質抽查', status: 'waiting' },
        { name: '回報老闆', status: 'waiting' },
      ],
      logs: notifications.slice(0, 10).map(n => ({
        time: (n.created_at || '').slice(11, 19),
        icon: '📋',
        message: `${n.title || ''} ${n.message || ''}`.slice(0, 80),
      })),
    },
    stats: {
      candidates: dbStats,
      jobs: jobStats,
      crawler: crawlerStats,
    },
  });
});

app.listen(PORT, () => {
  console.log(`🦞 龍蝦工作流程面板 → http://localhost:${PORT}`);
});
