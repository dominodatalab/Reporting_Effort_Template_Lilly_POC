from flask import render_template, jsonify
from app import app

@app.route('/')
def index():
    return render_template("report.html")
