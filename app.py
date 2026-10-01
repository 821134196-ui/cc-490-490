#!/usr/bin/env python3
"""本地启动命令：python3 app.py

首次启动自动建库、写入演示数据并生成 卡滞 / 换车 / 复检失败 样例，
随后启动进程内提醒线程（发车时限、维修超时）并监听 127.0.0.1:5000。
"""
from busdepot.web import create_app

if __name__ == "__main__":
    app = create_app(db_path="data/depot.db", photos_dir="photos")
    print("公交场站轮椅坡板点检与车辆放行系统")
    print("界面入口: http://127.0.0.1:5000/")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
