"""
Flask 頁面路由
"""
from flask import Blueprint, render_template

web_bp = Blueprint('web', __name__)


@web_bp.route('/')
def dashboard():
    return render_template('dashboard.html')


@web_bp.route('/tasks')
def tasks():
    return render_template('tasks.html')


@web_bp.route('/results')
def results():
    return render_template('results.html')


@web_bp.route('/logs')
def logs():
    return render_template('logs.html')


@web_bp.route('/settings')
def settings():
    return render_template('settings.html')
