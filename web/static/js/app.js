/**
 * HeadHunter Crawler 前端共用 JS
 */

// API 工具
async function api(path, options = {}) {
    const resp = await fetch('/api' + path, {
        headers: { 'Content-Type': 'application/json', ...options.headers },
        ...options,
    });
    return resp.json();
}

// 通知
function notify(message, type = 'info') {
    const div = document.createElement('div');
    const colors = {
        info: 'bg-blue-500',
        success: 'bg-green-500',
        error: 'bg-red-500',
        warning: 'bg-yellow-500',
    };
    div.className = `fixed top-16 right-4 ${colors[type]} text-white px-4 py-2 rounded-lg shadow-lg text-sm z-50 transition-opacity`;
    div.textContent = message;
    document.body.appendChild(div);
    setTimeout(() => {
        div.style.opacity = '0';
        setTimeout(() => div.remove(), 300);
    }, 3000);
}

// 日期格式化
function formatDate(dateStr) {
    if (!dateStr) return '--';
    const d = new Date(dateStr);
    return d.toLocaleDateString('zh-TW') + ' ' + d.toLocaleTimeString('zh-TW', { hour: '2-digit', minute: '2-digit' });
}
