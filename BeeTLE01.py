import io
import json
import base64
import smtplib
import sqlite3
from datetime import date, datetime, timedelta
from email.message import EmailMessage
import pandas as pd
from PIL import Image
import qrcode
import streamlit as st
import altair as alt

# ==========================================
# 1. 資料庫初始化與結構自動修復機制
# ==========================================
DB_FILE = "beetle_tracker.db"


def get_connection():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


BACKUP_TABLES = [
    "beetles",
    "logs",
    "notification_settings",
    "notification_recipients",
    "beetle_images",
]
BACKUP_REQUIRED_TABLES = [
    table_name for table_name in BACKUP_TABLES if table_name != "beetle_images"
]


def create_backup_payload():
    """建立包含所有系統資料的 JSON 備份內容。"""
    payload = {
        "format": "beetle_tracker_backup",
        "version": 1,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "tables": {},
    }
    with get_connection() as conn:
        for table_name in BACKUP_TABLES:
            rows = conn.execute(f'SELECT * FROM "{table_name}"').fetchall()
            records = []
            for row in rows:
                record = dict(row)
                if table_name == "beetle_images" and record.get("image_data"):
                    record["image_data"] = base64.b64encode(
                        record["image_data"]
                    ).decode("ascii")
                    record["image_data_encoding"] = "base64"
                records.append(record)
            payload["tables"][table_name] = records
    return payload


def restore_backup_payload(payload):
    """驗證並以交易方式還原完整 JSON 備份。"""
    if not isinstance(payload, dict):
        raise ValueError("備份格式無效。")
    if payload.get("format") != "beetle_tracker_backup":
        raise ValueError("不是本系統支援的備份檔案。")
    if not isinstance(payload.get("tables"), dict):
        raise ValueError("備份缺少 tables 資料。")
    missing_tables = [
        table_name for table_name in BACKUP_REQUIRED_TABLES
        if table_name not in payload["tables"]
    ]
    if missing_tables:
        raise ValueError(f"備份缺少資料表：{', '.join(missing_tables)}")

    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            for table_name in BACKUP_TABLES:
                columns = [
                    row[1]
                    for row in cursor.execute(
                        f'PRAGMA table_info("{table_name}")'
                    ).fetchall()
                ]
                table_records = payload["tables"].get(table_name, [])
                if not isinstance(table_records, list):
                    raise ValueError(f"資料表 {table_name} 格式無效。")
                cursor.execute(f'DELETE FROM "{table_name}"')
                for record in table_records:
                    if not isinstance(record, dict):
                        raise ValueError(f"資料表 {table_name} 含有無效資料列。")
                    record_columns = [
                        column for column in record if column in columns
                    ]
                    if not record_columns:
                        continue
                    record_values = [record[column] for column in record_columns]
                    if (
                        table_name == "beetle_images"
                        and record.get("image_data_encoding") == "base64"
                        and "image_data" in record_columns
                    ):
                        data_index = record_columns.index("image_data")
                        record_values[data_index] = base64.b64decode(
                            record_values[data_index]
                        )
                    placeholders = ", ".join("?" for _ in record_columns)
                    column_sql = ", ".join(
                        f'"{column}"' for column in record_columns
                    )
                    cursor.execute(
                        f'INSERT INTO "{table_name}" ({column_sql}) VALUES ({placeholders})',
                        record_values,
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def get_pending_maintenance_records():
    """取得目前需要換土或維護的個體清單。"""
    today = date.today()
    with get_connection() as conn:
        df_beetles = pd.read_sql_query("SELECT * FROM beetles", conn)
        df_logs = pd.read_sql_query("SELECT * FROM logs", conn)

    if "beetle_code" not in df_beetles.columns or df_beetles.empty:
        return []

    df_valid = df_beetles[
        df_beetles["beetle_code"].notna()
        & (df_beetles["beetle_code"].astype(str).str.strip() != "")
    ]
    df_active = df_valid[df_valid["current_stage"] != "死亡"]
    pending_list = []

    for _, beetle in df_active.iterrows():
        b_code = beetle.get("beetle_code")
        stage = beetle.get("current_stage", "未設定")
        if stage in ["蛹", "成蟲", "死亡"]:
            continue

        target_days = beetle.get("custom_maintenance_days")
        if pd.isna(target_days) or not target_days:
            target_days = 60
        target_days = int(target_days)

        b_logs = pd.DataFrame()
        if not df_logs.empty and "beetle_code" in df_logs.columns:
            b_logs = df_logs[df_logs["beetle_code"] == b_code].sort_values(
                "entry_date", ascending=False
            )

        maintenance_logs = b_logs
        if "maintenance_type" in maintenance_logs.columns:
            maintenance_logs = maintenance_logs[
                maintenance_logs["maintenance_type"] == "維護"
            ]

        if not maintenance_logs.empty:
            last_date_str = maintenance_logs.iloc[0]["entry_date"]
            try:
                last_date = datetime.strptime(last_date_str, "%Y-%m-%d").date()
                days_passed = (today - last_date).days
            except (TypeError, ValueError):
                last_date_str = "格式異常"
                days_passed = 999
        else:
            last_date_str = "尚未紀錄"
            days_passed = 999

        latest_log = b_logs.iloc[0] if not b_logs.empty else None
        last_length = latest_log.get("length_mm") if latest_log is not None else None
        last_weight = latest_log.get("weight_g") if latest_log is not None else None

        if days_passed >= target_days:
            pending_list.append(
                {
                    "個體編號": b_code,
                    "ID": beetle.get("custom_id", "-"),
                    "物種": beetle.get("species", "-"),
                    "當前階段": stage,
                    "個體提醒週期": f"{target_days} 天",
                    "上次換土日": last_date_str,
                    "已相隔天數": (
                        f"{days_passed} 天"
                        if days_passed != 999
                        else "未曾紀錄"
                    ),
                    "最新體長 (mm)": (
                        last_length if pd.notnull(last_length) else "-"
                    ),
                    "最新體重 (g)": (
                        last_weight if pd.notnull(last_weight) else "-"
                    ),
                }
            )
    return pending_list


def send_notification_email(settings, recipients, pending_list):
    """透過 SMTP 寄送待換土通知。"""
    message = EmailMessage()
    message["Subject"] = settings["subject"] or "甲蟲換土/維護提醒"
    message["From"] = settings["sender_email"] or settings["smtp_username"]
    message["To"] = ", ".join(recipients)

    lines = [
        "甲蟲飼育系統待換土/維護提醒",
        "",
        f"目前共有 {len(pending_list)} 隻個體達到維護條件。",
        "",
    ]
    for item in pending_list:
        lines.append(
            f"{item['個體編號']} | {item['物種']} | {item['當前階段']} | "
            f"上次換土：{item['上次換土日']} | 最新體長：{item['最新體長 (mm)']} | "
            f"最新體重：{item['最新體重 (g)']}"
        )
    message.set_content("\n".join(lines))

    if settings["smtp_ssl"]:
        with smtplib.SMTP_SSL(
            settings["smtp_host"], int(settings["smtp_port"]), timeout=20
        ) as smtp:
            if settings["smtp_username"]:
                smtp.login(settings["smtp_username"], settings["smtp_password"])
            smtp.send_message(message)
    else:
        with smtplib.SMTP(
            settings["smtp_host"], int(settings["smtp_port"]), timeout=20
        ) as smtp:
            smtp.starttls()
            if settings["smtp_username"]:
                smtp.login(settings["smtp_username"], settings["smtp_password"])
            smtp.send_message(message)


def init_db():
    """自動檢查資料庫結構，若缺少核心 beetle_code 欄位則安全重置重建"""
    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='beetles'"
        )
        table_exists = cursor.fetchone()

        if table_exists:
            cursor.execute("PRAGMA table_info(beetles)")
            cols = [row[1] for row in cursor.fetchall()]
            if "beetle_code" not in cols:
                cursor.execute("DROP TABLE beetles")
                cursor.execute("DROP TABLE IF EXISTS logs")

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS beetles (
                beetle_code TEXT PRIMARY KEY,
                custom_id TEXT NOT NULL,
                species TEXT NOT NULL,
                gender TEXT DEFAULT '未確定',
                origin TEXT,
                acquisition_source TEXT,
                initial_stage TEXT,
                current_stage TEXT DEFAULT '卵',
                hatch_date TEXT,
                parents_info TEXT,
                generation TEXT,
                lineage TEXT,
                father_id TEXT,
                mother_id TEXT,
                notes TEXT,
                custom_maintenance_days INTEGER DEFAULT 60,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        cursor.execute("PRAGMA table_info(beetles)")
        beetle_cols = [row[1] for row in cursor.fetchall()]
        if "acquisition_source" not in beetle_cols:
            cursor.execute(
                "ALTER TABLE beetles ADD COLUMN acquisition_source TEXT"
            )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                beetle_code TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                length_mm REAL,
                weight_g REAL,
                substrate_type TEXT,
                container_size_ml INTEGER,
                notes TEXT,
                maintenance_type TEXT DEFAULT '一般紀錄'
            )
        """
        )
        cursor.execute("PRAGMA table_info(logs)")
        log_cols = [row[1] for row in cursor.fetchall()]
        if "maintenance_type" not in log_cols:
            cursor.execute(
                "ALTER TABLE logs ADD COLUMN maintenance_type TEXT DEFAULT '一般紀錄'"
            )
            cursor.execute(
                """
                UPDATE logs
                SET maintenance_type='維護'
                WHERE notes LIKE '%換土%'
                   OR notes LIKE '%換菌%'
                   OR notes LIKE '%轉木屑%'
                """
            )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 0,
                notification_days INTEGER NOT NULL DEFAULT 1,
                smtp_host TEXT NOT NULL DEFAULT '',
                smtp_port INTEGER NOT NULL DEFAULT 587,
                smtp_ssl INTEGER NOT NULL DEFAULT 0,
                smtp_username TEXT NOT NULL DEFAULT '',
                smtp_password TEXT NOT NULL DEFAULT '',
                sender_email TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL DEFAULT '甲蟲換土/維護提醒',
                last_sent_at TEXT
            )
            """
        )
        cursor.execute(
            "INSERT OR IGNORE INTO notification_settings (id) VALUES (1)"
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_recipients (
                slot INTEGER PRIMARY KEY,
                email TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        for slot in range(1, 11):
            cursor.execute(
                "INSERT OR IGNORE INTO notification_recipients (slot) VALUES (?)",
                (slot,),
            )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS beetle_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                beetle_code TEXT NOT NULL,
                file_name TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                image_data BLOB NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def seed_sample_data():
    """載入測試範例資料"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM beetles")
        if cursor.fetchone()[0] == 0:
            today = date.today()
            sample_beetles = [
                (
                    "2026-DHH-01",
                    "DHH-M01",
                    "赫克力士長角大カブト",
                    "公",
                    "瓜地馬拉",
                    "自繁",
                    "一齡幼蟲",
                    "三齡幼蟲",
                    "2025-08-15",
                    "170mm極太系",
                    "CBF1",
                    "極太血統",
                    "F-170",
                    "M-075",
                    "吃食量大，食痕正常",
                    60,
                ),
                (
                    "2026-彩虹-01",
                    "RB-01",
                    "彩虹鍬形蟲",
                    "母",
                    "澳洲昆士蘭",
                    "購入",
                    "卵",
                    "二齡幼蟲",
                    "2025-11-01",
                    "綠彩自留",
                    "WF2",
                    "綠彩系",
                    "RB-F02",
                    "RB-M01",
                    "狀態安定",
                    45,
                ),
            ]
            cursor.executemany(
                """
                INSERT INTO beetles (
                    beetle_code, custom_id, species, gender, origin, acquisition_source, initial_stage, 
                    current_stage, hatch_date, parents_info, generation, lineage, 
                    father_id, mother_id, notes, custom_maintenance_days
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                sample_beetles,
            )

            d1 = (today - timedelta(days=90)).strftime("%Y-%m-%d")
            d2 = (today - timedelta(days=30)).strftime("%Y-%m-%d")

            sample_logs = [
                ("2026-DHH-01", d1, 35.0, 45.2, "大夢培植菌瓶", 1400, "換土", "維護"),
                ("2026-DHH-01", d2, 65.0, 88.5, "二次發酵木屑", 2000, "轉木屑", "維護"),
                ("2026-彩虹-01", d1, 12.0, 5.5, "高發酵木屑", 500, "換菌", "維護"),
            ]
            cursor.executemany(
                """
                INSERT INTO logs (beetle_code, entry_date, length_mm, weight_g, substrate_type, container_size_ml, notes, maintenance_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                sample_logs,
            )
            conn.commit()


# ==========================================
# 2. QR Code 生成工具
# ==========================================
def generate_qrcode(beetle_data: dict) -> Image.Image:
    """生成包含甲蟲資料的 QR Code 圖檔"""
    qr_json = json.dumps(beetle_data, ensure_ascii=False, default=str)
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_json)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white")


# ==========================================
# 3. Streamlit 主程式介面
# ==========================================
st.set_page_config(
    page_title="甲蟲專業飼育紀錄系統",
    layout="wide",
)

init_db()

# 初始化 Session State
if "edit_target_code" not in st.session_state:
    st.session_state.edit_target_code = None
if "current_action" not in st.session_state:
    st.session_state.current_action = None
if "edit_log_rows" not in st.session_state:
    st.session_state.edit_log_rows = 1
if "confirm_clear_database" not in st.session_state:
    st.session_state.confirm_clear_database = False

st.markdown(
    """
    <style>
    section[data-testid="stSidebar"] [data-testid="stSegmentedControl"] button,
    section[data-testid="stSidebar"] [data-testid="stButtonGroup"] button,
    section[data-testid="stSidebar"] [data-baseweb="button-group"] button,
    section[data-testid="stSidebar"] [role="radiogroup"] button,
    section[data-testid="stSidebar"] [data-testid="stButton"] button {
        border: 1px solid #c7cdd6 !important;
        border-radius: 6px !important;
        width: 100% !important;
        min-width: 100% !important;
        height: 42px !important;
        min-height: 42px !important;
        box-sizing: border-box !important;
        padding: 0 10px !important;
        line-height: 1.2 !important;
        white-space: nowrap !important;
        overflow: hidden !important;
        text-overflow: ellipsis !important;
        box-shadow: none !important;
        outline: none !important;
    }

    section[data-testid="stSidebar"] [data-testid="stSegmentedControl"],
    section[data-testid="stSidebar"] [data-testid="stButtonGroup"],
    section[data-testid="stSidebar"] [data-baseweb="button-group"] {
        width: 100% !important;
    }

    section[data-testid="stSidebar"] [data-testid="stSegmentedControl"] button[aria-pressed="true"],
    section[data-testid="stSidebar"] [data-testid="stButtonGroup"] button[aria-pressed="true"],
    section[data-testid="stSidebar"] [data-baseweb="button-group"] button[aria-pressed="true"],
    section[data-testid="stSidebar"] [role="radio"][aria-checked="true"],
    section[data-testid="stSidebar"] button[aria-pressed="true"],
    section[data-testid="stSidebar"] [aria-checked="true"],
    section[data-testid="stSidebar"] [data-selected="true"] {
        border-color: #c7cdd6 !important;
        background-color: #dbeafe !important;
        color: #1d4ed8 !important;
        box-shadow: none !important;
        outline: none !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.sidebar.title("系統功能選單")

def clear_action_on_menu_change():
    st.session_state.current_action = None
    st.session_state.edit_target_code = None


menu_left, menu_center, menu_right = st.sidebar.columns([0.05, 0.9, 0.05])
menu = menu_center.segmented_control(
    "",
    [
        "全場總覽與待換土提醒",
        "個體清單與檔案管理",
        "新增個體與成長紀錄",
        "QR Code 掃描與識別",
        "通知管理",
        "備份/匯入",
    ],
    default="全場總覽與待換土提醒",
    key="main_menu",
    on_change=clear_action_on_menu_change,
)

st.sidebar.markdown("---")
if st.sidebar.button("載入測試示範資料", use_container_width=True):
    seed_sample_data()
    st.sidebar.success("示範資料已載入！")
    st.rerun()

if st.sidebar.button("清空所有資料庫紀錄", use_container_width=True):
    st.session_state.confirm_clear_database = True

if st.session_state.confirm_clear_database:
    st.sidebar.warning("此操作會刪除所有個體、成長紀錄與通知設定，確定要繼續嗎？")
    confirm_col1, confirm_col2 = st.sidebar.columns(2)
    if confirm_col1.button("確認清空", type="primary"):
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM beetles")
            cursor.execute("DELETE FROM logs")
            cursor.execute("DELETE FROM notification_settings")
            cursor.execute("DELETE FROM notification_recipients")
            cursor.execute("DELETE FROM beetle_images")
            conn.commit()
        st.session_state.confirm_clear_database = False
        st.sidebar.success("資料庫已全數清空！")
        st.rerun()
    if confirm_col2.button("取消"):
        st.session_state.confirm_clear_database = False
        st.rerun()

# ==========================================
# 頁面 1: 全場總覽與待換土提醒
# ==========================================
if menu == "全場總覽與待換土提醒":
    st.title("全場總覽與待換土提醒")

    with get_connection() as conn:
        df_beetles = pd.read_sql_query("SELECT * FROM beetles", conn)
        df_logs = pd.read_sql_query("SELECT * FROM logs", conn)

    if "beetle_code" in df_beetles.columns and not df_beetles.empty:
        df_valid = df_beetles[
            df_beetles["beetle_code"].notna()
            & (df_beetles["beetle_code"].astype(str).str.strip() != "")
        ]
        df_active = df_valid[df_valid["current_stage"] != "死亡"]
    else:
        df_valid = pd.DataFrame()
        df_active = pd.DataFrame()

    total_active_beetles = len(df_active)

    if not df_active.empty and "current_stage" in df_active.columns:
        larvae_cnt = len(
            df_active[
                df_active["current_stage"].isin(
                    ["一齡幼蟲", "二齡幼蟲", "三齡幼蟲"]
                )
            ]
        )
        pupa_cnt = len(df_active[df_active["current_stage"] == "蛹"])
        adult_cnt = len(df_active[df_active["current_stage"] == "成蟲"])
    else:
        larvae_cnt, pupa_cnt, adult_cnt = 0, 0, 0

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("總列管數量 (活體)", f"{total_active_beetles} 隻")
    col2.metric("幼蟲數量", f"{larvae_cnt} 隻")
    col3.metric("化蛹數量", f"{pupa_cnt} 隻")
    col4.metric("成蟲數量", f"{adult_cnt} 隻")

    st.markdown("---")
    st.subheader("待換土/維護提醒")

    pending_list = []
    today = date.today()

    if not df_active.empty:
        for _, beetle in df_active.iterrows():
            b_code = beetle.get("beetle_code")
            stage = beetle.get("current_stage", "未設定")

            if stage in ["蛹", "成蟲", "死亡"]:
                continue

            b_target_days = beetle.get("custom_maintenance_days")
            if pd.isna(b_target_days) or not b_target_days:
                b_target_days = 60
            b_target_days = int(b_target_days)

            b_logs = pd.DataFrame()
            if not df_logs.empty and "beetle_code" in df_logs.columns:
                b_logs = df_logs[df_logs["beetle_code"] == b_code].sort_values(
                    "entry_date", ascending=False
                )
            maintenance_logs = b_logs
            if "maintenance_type" in maintenance_logs.columns:
                maintenance_logs = maintenance_logs[
                    maintenance_logs["maintenance_type"] == "維護"
                ]

            if not maintenance_logs.empty:
                last_date_str = maintenance_logs.iloc[0]["entry_date"]
                try:
                    last_date = datetime.strptime(
                        last_date_str, "%Y-%m-%d"
                    ).date()
                    days_passed = (today - last_date).days
                except ValueError:
                    last_date_str = "格式異常"
                    days_passed = 999
            else:
                last_date_str = "尚未紀錄"
                days_passed = 999

            if not b_logs.empty:
                last_weight = b_logs.iloc[0].get("weight_g")
                last_length = b_logs.iloc[0].get("length_mm")
            else:
                last_weight = None
                last_length = None

            if days_passed >= b_target_days:
                pending_list.append(
                    {
                        "個體編號": b_code,
                        "ID": beetle.get("custom_id", "-"),
                        "物種": beetle.get("species", "-"),
                        "當前階段": stage,
                        "專屬提醒週期": f"{b_target_days} 天",
                        "上次換土日": last_date_str,
                        "已相隔天數": (
                            f"{days_passed} 天"
                            if days_passed != 999
                            else "未曾紀錄"
                        ),
                        "最新體長 (mm)": (
                            last_length if pd.notnull(last_length) else "-"
                        ),
                        "最新體重 (g)": (
                            last_weight if pd.notnull(last_weight) else "-"
                        ),
                    }
                )

    if pending_list:
        st.warning(
            f"目前共有 **{len(pending_list)}** 隻個體已達到換土/維護條件！"
        )
        st.dataframe(pd.DataFrame(pending_list), use_container_width=True)
    elif total_active_beetles == 0:
        st.info(
            "目前資料庫中沒有任何活體甲蟲檔案，請點擊左側「載入測試示範資料」或前往「新增個體」分頁建立。"
        )
    else:
        st.success("全場狀況良好，目前沒有到達換土週期的個體！")

# ==========================================
# 頁面 2: 個體清單與檔案管理
# ==========================================
elif menu == "個體清單與檔案管理":
    st.title("個體清單與檔案管理")

    with get_connection() as conn:
        df_beetles = pd.read_sql_query("SELECT * FROM beetles", conn)
        df_logs = pd.read_sql_query("SELECT * FROM logs", conn)

    if "beetle_code" in df_beetles.columns and not df_beetles.empty:
        df_valid = df_beetles[
            df_beetles["beetle_code"].notna()
            & (df_beetles["beetle_code"].astype(str).str.strip() != "")
        ]
    else:
        df_valid = pd.DataFrame()

    def clear_list_search_state():
        st.session_state.current_action = None
        st.session_state.edit_target_code = None
        st.session_state.beetle_list_page = 1

    search_col1, search_col2, search_col3, search_col4 = st.columns(4)
    search_id = search_col1.text_input(
        "ID 搜尋",
        placeholder="輸入 ID",
        key="beetle_search_id",
        on_change=clear_list_search_state,
    )
    search_species = search_col2.text_input(
        "物種搜尋",
        placeholder="輸入物種",
        key="beetle_search_species",
        on_change=clear_list_search_state,
    )
    search_gender = search_col3.selectbox(
        "性別搜尋",
        ["全部", "未確定", "公", "母"],
        key="beetle_search_gender",
        on_change=clear_list_search_state,
    )
    search_source = search_col4.text_input(
        "取得來源搜尋",
        placeholder="輸入取得來源",
        key="beetle_search_source",
        on_change=clear_list_search_state,
    )

    for column_name, search_value in [
        ("custom_id", search_id),
        ("species", search_species),
        ("acquisition_source", search_source),
    ]:
        if search_value.strip() and column_name in df_valid.columns:
            df_valid = df_valid[
                df_valid[column_name]
                .fillna("")
                .astype(str)
                .str.contains(search_value.strip(), case=False, na=False)
            ]

    if search_gender != "全部" and "gender" in df_valid.columns:
        df_valid = df_valid[
            df_valid["gender"].fillna("").astype(str) == search_gender
        ]

    if df_valid.empty:
        st.info("查無符合條件的個體資料。")
    else:
        st.markdown("### 列管甲蟲清單")

        target_display_cols = [
            "beetle_code",
            "custom_id",
            "species",
            "gender",
            "acquisition_source",
            "current_stage",
            "lineage",
            "hatch_date",
        ]
        available_cols = [
            c for c in target_display_cols if c in df_valid.columns
        ]

        col_names = {
            "beetle_code": "個體編號",
            "custom_id": "ID",
            "species": "物種",
            "gender": "性別",
            "acquisition_source": "取得來源",
            "current_stage": "當前階段",
            "lineage": "血統",
            "hatch_date": "孵化/採收日期",
        }

        st.markdown("### 個體操作選項")
        page_size = 10
        total_pages = max(1, (len(df_valid) + page_size - 1) // page_size)

        def clear_action_on_page_change():
            st.session_state.current_action = None
            st.session_state.edit_target_code = None

        page_number = st.number_input(
            "頁次",
            min_value=1,
            max_value=total_pages,
            value=min(
                int(st.session_state.get("beetle_list_page", 1)), total_pages
            ),
            step=1,
            key="beetle_list_page",
            on_change=clear_action_on_page_change,
        )

        page_start = (page_number - 1) * page_size
        page_df = df_valid.iloc[page_start : page_start + page_size]
        st.caption(f"第 {page_number} / {total_pages} 頁，共 {len(df_valid)} 筆")

        header_cols = st.columns(
            [1.5, 1, 1.7, 0.7, 1.1, 1.3, 1.2, 1.2, 5]
        )
        for header_col, header in zip(
            header_cols,
            [
                "個體編號",
                "ID",
                "物種",
                "性別",
                "取得來源",
                "當前階段",
                "血統",
                "孵化/採收日期",
                "操作選項",
            ],
        ):
            header_col.markdown(f"**{header}**")

        for row_number, (_, beetle) in enumerate(page_df.iterrows()):
            row_code = beetle["beetle_code"]
            row_cols = st.columns(
                [1.5, 1, 1.7, 0.7, 1.1, 1.3, 1.2, 1.2, 5]
            )
            row_cols[0].write(row_code)
            row_cols[1].write(beetle.get("custom_id") or "-")
            row_cols[2].write(beetle.get("species") or "-")
            row_cols[3].write(beetle.get("gender") or "-")
            row_cols[4].write(beetle.get("acquisition_source") or "-")
            row_cols[5].write(beetle.get("current_stage") or "-")
            row_cols[6].write(beetle.get("lineage") or "-")
            row_cols[7].write(beetle.get("hatch_date") or "-")

            action_key = f"beetle_{page_start + row_number}_{row_code}"
            with row_cols[8].popover("操作選項"):
                action_cols = st.columns(6)
                if action_cols[0].button("查看", key=f"view_{action_key}", use_container_width=True):
                    st.session_state.current_action = "view"
                    st.session_state.edit_target_code = row_code
                if action_cols[1].button("編輯", key=f"edit_{action_key}"):
                    st.session_state.current_action = "edit"
                    st.session_state.edit_target_code = row_code
                    existing_logs_cnt = (
                        len(df_logs[df_logs["beetle_code"] == row_code])
                        if not df_logs.empty
                        else 0
                    )
                    st.session_state.edit_log_rows = max(1, existing_logs_cnt)
                if action_cols[2].button("曲線", key=f"chart_{action_key}", use_container_width=True):
                    st.session_state.current_action = "chart"
                    st.session_state.edit_target_code = row_code
                if action_cols[3].button("QR", key=f"qr_{action_key}"):
                    st.session_state.current_action = "qr"
                    st.session_state.edit_target_code = row_code
                if action_cols[4].button("刪除", key=f"delete_{action_key}", use_container_width=True):
                    st.session_state.current_action = "delete"
                    st.session_state.edit_target_code = row_code
                if action_cols[5].button("圖片", key=f"images_{action_key}", use_container_width=True):
                    st.session_state.current_action = "images"
                    st.session_state.edit_target_code = row_code

        @st.dialog("個體操作")
        def render_action_dialog():
            # 當前的操作對象資訊
            active_code = st.session_state.get("edit_target_code")
            matching_beetles = df_valid[df_valid["beetle_code"] == active_code]

            if not matching_beetles.empty:
                selected_info = matching_beetles.iloc[0]

                # 1. 查看詳情
                if st.session_state.get("current_action") == "view":
                    st.info(f"個體詳細檔案：{active_code}")
                    v_col1, v_col2, v_col3 = st.columns(3)
                    v_col1.write(f"**個體編號:** {selected_info.get('beetle_code')}")
                    v_col1.write(f"**ID:** {selected_info.get('custom_id', '無')}")
                    v_col1.write(f"**物種名稱:** {selected_info.get('species')}")
                    v_col1.write(f"**性別:** {selected_info.get('gender')}")

                    v_col2.write(f"**產地:** {selected_info.get('origin') or '無'}")
                    v_col2.write(
                        f"**取得來源:** {selected_info.get('acquisition_source') or '無'}"
                    )
                    v_col2.write(
                        f"**初始階段:** {selected_info.get('initial_stage') or '無'}"
                    )
                    v_col2.write(
                        f"**當前階段:** {selected_info.get('current_stage') or '無'}"
                    )
                    v_col2.write(
                        f"**孵化/採收日期:** {selected_info.get('hatch_date') or '無'}"
                    )

                    v_col3.write(
                        f"**親代:** {selected_info.get('parents_info') or '無'}"
                    )
                    v_col3.write(
                        f"**累代:** {selected_info.get('generation') or '無'}"
                    )
                    v_col3.write(
                        f"**血統:** {selected_info.get('lineage') or '無'}"
                    )
                    v_col3.write(
                        f"**父/母 ID:** {selected_info.get('father_id') or '無'} / {selected_info.get('mother_id') or '無'}"
                    )

                    st.write(
                        f"**個體換土提醒週期:** {selected_info.get('custom_maintenance_days') or 60} 天"
                    )
                    st.write(f"**備註:** {selected_info.get('notes') or '無'}")

                    # 顯示成長歷史紀錄（唯讀）
                    b_logs = pd.DataFrame()
                    if not df_logs.empty and "beetle_code" in df_logs.columns:
                        b_logs = df_logs[
                            df_logs["beetle_code"] == active_code
                        ].sort_values("entry_date", ascending=False)

                    st.markdown("---")
                    st.markdown("##### 成長歷史紀錄 (唯讀)")
                    if b_logs.empty:
                        st.info("該個體尚無成長歷史紀錄。")
                    else:
                        display_df = b_logs[["entry_date", "length_mm", "weight_g", "notes"]].rename(
                            columns={
                                "entry_date": "日期",
                                "length_mm": "體長(mm)",
                                "weight_g": "體重(g)",
                                "notes": "備註",
                            }
                        )
                        st.dataframe(display_df, use_container_width=True)

                # 2. 編輯資料
                elif st.session_state.get("current_action") == "edit":
                    st.subheader(f"編輯個體資料：{active_code}")

                    # 提取當前 Log 歷史紀錄
                    b_logs_list = []
                    if not df_logs.empty and "beetle_code" in df_logs.columns:
                        b_logs_list = (
                            df_logs[df_logs["beetle_code"] == active_code]
                            .sort_values("entry_date")
                            .to_dict("records")
                        )

                    # 防呆：確保編輯數量至少為 1
                    if st.session_state.edit_log_rows < 1:
                        st.session_state.edit_log_rows = max(1, len(b_logs_list))

                    with st.form("edit_beetle_form_stable"):
                        st.markdown("##### 基本資訊編輯")
                        e_c1, e_c2, e_c3 = st.columns(3)
                        edit_beetle_code = e_c1.text_input(
                            "個體編號 (主鍵, 必填)",
                            selected_info.get("beetle_code", ""),
                        )
                        edit_custom_id = e_c2.text_input(
                            "ID (必填)", selected_info.get("custom_id", "")
                        )
                        edit_species = e_c3.text_input(
                            "物種名稱 (必填)", selected_info.get("species", "")
                        )

                        gender_val = selected_info.get("gender", "未確定")
                        gender_idx = (
                            ["未確定", "公", "母"].index(gender_val)
                            if gender_val in ["未確定", "公", "母"]
                            else 0
                        )
                        e_c4, e_c5, e_c6 = st.columns(3)
                        edit_gender = e_c4.selectbox(
                            "性別", ["未確定", "公", "母"], index=gender_idx
                        )

                        stages = [
                            "卵",
                            "一齡幼蟲",
                            "二齡幼蟲",
                            "三齡幼蟲",
                            "前蛹",
                            "蛹",
                            "成蟲",
                            "死亡",
                        ]
                        stage_val = selected_info.get("current_stage", "卵")
                        stage_idx = (
                            stages.index(stage_val) if stage_val in stages else 0
                        )
                        edit_stage = e_c5.selectbox(
                            "當前階段", stages, index=stage_idx
                        )

                        init_stage_val = selected_info.get("initial_stage", "卵")
                        init_stage_idx = (
                            stages.index(init_stage_val)
                            if init_stage_val in stages
                            else 0
                        )
                        edit_initial_stage = e_c6.selectbox(
                            "初始階段", stages, index=init_stage_idx
                        )

                        e_c7, e_c8, e_c9 = st.columns(3)
                        edit_origin = e_c7.text_input(
                            "產地", selected_info.get("origin") or ""
                        )
                        edit_acquisition_source = st.text_input(
                            "取得來源 (選填)",
                            selected_info.get("acquisition_source") or "",
                        )

                        curr_hatch_str = selected_info.get("hatch_date")
                        try:
                            curr_hatch_dt = (
                                datetime.strptime(
                                    curr_hatch_str, "%Y-%m-%d"
                                ).date()
                                if curr_hatch_str
                                else date.today()
                            )
                        except Exception:
                            curr_hatch_dt = date.today()

                        edit_hatch_date = e_c8.date_input(
                            "孵化/採收日期", curr_hatch_dt
                        )
                        edit_parents_info = e_c9.text_input(
                            "親代", selected_info.get("parents_info") or ""
                        )

                        e_c10, e_c11, e_c12 = st.columns(3)
                        edit_generation = e_c10.text_input(
                            "累代", selected_info.get("generation") or ""
                        )
                        edit_lineage = e_c11.text_input(
                            "血統", selected_info.get("lineage") or ""
                        )
                        cur_m_days = selected_info.get("custom_maintenance_days")
                        edit_m_days = e_c12.number_input(
                            "個體換土提醒週期 (天)",
                            value=(
                                int(cur_m_days)
                                if pd.notnull(cur_m_days) and cur_m_days
                                else 60
                            ),
                            step=5,
                        )

                        e_c13, e_c14 = st.columns(2)
                        edit_father_id = e_c13.text_input(
                            "父本 ID", selected_info.get("father_id") or ""
                        )
                        edit_mother_id = e_c14.text_input(
                            "母本 ID", selected_info.get("mother_id") or ""
                        )

                        edit_notes = st.text_area(
                            "備註", selected_info.get("notes") or ""
                        )

                        st.markdown("---")
                        st.markdown("##### 成長歷史紀錄編輯明細")

                        log_header_cols = st.columns([1.6, 1.6, 1.6, 1.5, 3])
                        log_headers = [
                            "日期",
                            "體長 (mm)",
                            "體重 (g)",
                            "換土/維護",
                            "備註",
                        ]
                        for header_col, header in zip(
                            log_header_cols, log_headers
                        ):
                            header_col.caption(header)

                        edited_logs = []
                        for i in range(st.session_state.edit_log_rows):
                            log_item = (
                                b_logs_list[i] if i < len(b_logs_list) else {}
                            )
                            lc1, lc2, lc3, lc4, lc5 = st.columns(
                                [1.6, 1.6, 1.6, 1.5, 3]
                            )

                            try:
                                def_date = (
                                    datetime.strptime(
                                        log_item.get("entry_date"), "%Y-%m-%d"
                                    ).date()
                                    if log_item.get("entry_date")
                                    else date.today()
                                )
                            except Exception:
                                def_date = date.today()

                            l_date = lc1.date_input(
                                f"#{i+1}", def_date, key=f"el_d_{i}"
                            )
                            l_len = lc2.number_input(
                                f"#{i+1}",
                                min_value=0.0,
                                value=float(log_item.get("length_mm") or 0.0),
                                step=0.1,
                                key=f"el_len_{i}",
                            )
                            l_wt = lc3.number_input(
                                f"#{i+1}",
                                min_value=0.0,
                                value=float(log_item.get("weight_g") or 0.0),
                                step=0.1,
                                key=f"el_w_{i}",
                            )
                            legacy_maintenance = any(
                                keyword in str(log_item.get("notes") or "")
                                for keyword in ["換土", "換菌", "轉木屑"]
                            )
                            maintenance_default = (
                                log_item.get("maintenance_type") == "維護"
                                or legacy_maintenance
                            )
                            l_maintenance = lc4.checkbox(
                                f"#{i+1}",
                                value=maintenance_default,
                                key=f"el_m_{i}",
                            )
                            l_note = lc5.text_input(
                                f"#{i+1}",
                                value=str(log_item.get("notes") or ""),
                                key=f"el_n_{i}",
                            )

                            edited_logs.append(
                                {
                                    "date": l_date.strftime("%Y-%m-%d"),
                                    "length": l_len if l_len > 0 else None,
                                    "weight": l_wt if l_wt > 0 else None,
                                    "maintenance": l_maintenance,
                                    "notes": l_note,
                                }
                            )

                        st.markdown("---")

                        # ========== 調整按鈕位置至此處（儲存按鈕上方） ==========
                        row_c1, row_c2, _ = st.columns([2, 2, 6])
                        btn_add_log = row_c1.form_submit_button(
                            "新增紀錄", type="secondary"
                        )
                        btn_del_log = row_c2.form_submit_button(
                            "移除紀錄", type="secondary"
                        )

                        btn_save = st.form_submit_button(
                            "儲存所有修改", type="primary"
                        )

                        # 動態列數邏輯處理
                        if btn_add_log:
                            st.session_state.edit_log_rows += 1
                            st.rerun()

                        if btn_del_log:
                            if st.session_state.edit_log_rows > 1:
                                st.session_state.edit_log_rows -= 1
                                st.rerun()

                        # 儲存邏輯處理
                        if btn_save:
                            if (
                                not edit_beetle_code
                                or not edit_custom_id
                                or not edit_species
                            ):
                                st.error(
                                    "「個體編號」、「ID」與「物種名稱」為必填欄位！"
                                )
                            else:
                                try:
                                    conn = get_connection()
                                    cursor = conn.cursor()

                                    # 更新主表
                                    cursor.execute(
                                        """
                                        UPDATE beetles 
                                        SET beetle_code=?, custom_id=?, species=?, gender=?, origin=?, acquisition_source=?, 
                                            initial_stage=?, current_stage=?, hatch_date=?, parents_info=?, 
                                            generation=?, lineage=?, father_id=?, mother_id=?, notes=?, 
                                            custom_maintenance_days=?
                                        WHERE beetle_code=?
                                    """,
                                        (
                                            edit_beetle_code,
                                            edit_custom_id,
                                            edit_species,
                                            edit_gender,
                                            edit_origin,
                                            edit_acquisition_source,
                                            edit_initial_stage,
                                            edit_stage,
                                            edit_hatch_date.strftime("%Y-%m-%d"),
                                            edit_parents_info,
                                            edit_generation,
                                            edit_lineage,
                                            edit_father_id,
                                            edit_mother_id,
                                            edit_notes,
                                            edit_m_days,
                                            active_code,
                                        ),
                                    )

                                    # 清除舊 Log 並寫入修改後的 Log 數據
                                    cursor.execute(
                                        "DELETE FROM logs WHERE beetle_code=?",
                                        (edit_beetle_code,),
                                    )
                                    if edit_beetle_code != active_code:
                                        cursor.execute(
                                            "DELETE FROM logs WHERE beetle_code=?",
                                            (active_code,),
                                        )

                                    for elog in edited_logs:
                                        if (
                                            elog["length"]
                                            or elog["weight"]
                                            or elog["notes"]
                                        ):
                                            cursor.execute(
                                                """
                                                INSERT INTO logs (beetle_code, entry_date, length_mm, weight_g, notes, maintenance_type)
                                                VALUES (?, ?, ?, ?, ?, ?)
                                            """,
                                                (
                                                    edit_beetle_code,
                                                    elog["date"],
                                                    elog["length"],
                                                    elog["weight"],
                                                    elog["notes"],
                                                    "維護" if elog["maintenance"] else "一般紀錄",
                                                ),
                                            )

                                    conn.commit()
                                    conn.close()

                                    st.session_state.edit_target_code = (
                                        edit_beetle_code
                                    )
                                    st.session_state.current_action = "view"
                                    st.success("資料與成長紀錄修改成功並已儲存！")
                                    st.rerun()

                                except sqlite3.IntegrityError:
                                    st.error(
                                        f"儲存失敗：個體編號 `{edit_beetle_code}` 已存在或有重複資料！"
                                    )
                                except Exception as ex:
                                    st.error(f"資料庫更新發生錯誤：{ex}")

                # 3. 成長曲線
                elif st.session_state.get("current_action") == "chart":
                    st.subheader(f"{active_code} 成長曲線圖表")
                    b_logs = pd.DataFrame()
                    if not df_logs.empty and "beetle_code" in df_logs.columns:
                        b_logs = df_logs[
                            df_logs["beetle_code"] == active_code
                        ].sort_values("entry_date")

                    if b_logs.empty:
                        st.warning("該個體尚未新增任何成長紀錄，無法產生曲線。")
                    else:
                            b_logs["entry_date"] = pd.to_datetime(b_logs["entry_date"])
                            b_logs = b_logs.sort_values("entry_date")

                            chart_col1, chart_col2 = st.columns(2)
                            with chart_col1:
                                st.markdown("#### 秤重趨勢 (g)")
                                if (
                                    "weight_g" not in b_logs.columns
                                    or b_logs["weight_g"].dropna().empty
                                ):
                                    st.caption("無體重數據")
                                else:
                                    df_w = b_logs[["entry_date", "weight_g"]].dropna()
                                    line_w = (
                                        alt.Chart(df_w)
                                        .mark_line(interpolate="monotone", point=False)
                                        .encode(
                                            x=alt.X("entry_date:T", title="日期"),
                                            y=alt.Y("weight_g:Q", title="體重 (g)"),
                                            tooltip=[
                                                alt.Tooltip("entry_date:T", title="日期"),
                                                alt.Tooltip("weight_g:Q", title="體重 (g)"),
                                            ],
                                        )
                                    )
                                    points_w = alt.Chart(df_w).mark_point(size=40).encode(
                                        x="entry_date:T",
                                        y="weight_g:Q",
                                        tooltip=[
                                            alt.Tooltip("entry_date:T", title="日期"),
                                            alt.Tooltip("weight_g:Q", title="體重 (g)"),
                                        ],
                                    )
                                    st.altair_chart((line_w + points_w).properties(height=300), use_container_width=True)

                            with chart_col2:
                                st.markdown("#### 體長趨勢 (mm)")
                                if (
                                    "length_mm" not in b_logs.columns
                                    or b_logs["length_mm"].dropna().empty
                                ):
                                    st.caption("無體長數據")
                                else:
                                    df_l = b_logs[["entry_date", "length_mm"]].dropna()
                                    line_l = (
                                        alt.Chart(df_l)
                                        .mark_line(interpolate="monotone", point=False)
                                        .encode(
                                            x=alt.X("entry_date:T", title="日期"),
                                            y=alt.Y("length_mm:Q", title="體長 (mm)"),
                                            tooltip=[
                                                alt.Tooltip("entry_date:T", title="日期"),
                                                alt.Tooltip("length_mm:Q", title="體長 (mm)"),
                                            ],
                                        )
                                    )
                                    points_l = alt.Chart(df_l).mark_point(size=40).encode(
                                        x="entry_date:T",
                                        y="length_mm:Q",
                                        tooltip=[
                                            alt.Tooltip("entry_date:T", title="日期"),
                                            alt.Tooltip("length_mm:Q", title="體長 (mm)"),
                                        ],
                                    )
                                    st.altair_chart((line_l + points_l).properties(height=300), use_container_width=True)

                # 4. QR Code
                elif st.session_state.get("current_action") == "qr":
                    st.subheader(f"{active_code} 專屬 QR Code")
                    qr_payload = {
                        key: value
                        for key, value in selected_info.to_dict().items()
                        if pd.notna(value)
                    }
                    qr_img = generate_qrcode(qr_payload)

                    buf = io.BytesIO()
                    qr_img.save(buf, format="PNG")
                    byte_im = buf.getvalue()

                    st.image(
                        byte_im,
                        caption=f"掃描以取得 {active_code} 的檔案",
                        width=300,
                    )
                    st.info(
                        "**使用說明：**\n可將此 QR Code 列印並貼在飼育瓶/箱身。用手機相機掃描或於此系統「QR Code 掃描與識別」頁面即可讀取個體資料。"
                    )

                # 5. 個體圖片管理
                elif st.session_state.get("current_action") == "images":
                    st.subheader(f"{active_code} 圖片管理")
                    image_upload_version_key = f"image_upload_version_{active_code}"
                    image_upload_version = st.session_state.get(
                        image_upload_version_key, 0
                    )
                    image_upload_key = (
                        f"image_upload_{active_code}_{image_upload_version}"
                    )
                    image_upload_success_key = f"image_upload_success_{active_code}"
                    if st.session_state.pop(image_upload_success_key, False):
                        st.success("圖片已上傳，新增圖片欄位已清空。")

                    uploaded_images = st.file_uploader(
                        "新增圖片",
                        type=["png", "jpg", "jpeg", "webp"],
                        accept_multiple_files=True,
                        key=image_upload_key,
                    )
                    if st.button(
                        "上傳圖片",
                        type="primary",
                        disabled=not uploaded_images,
                        key=f"upload_images_{active_code}_{image_upload_version}",
                    ):
                        with get_connection() as conn:
                            conn.executemany(
                                """
                                INSERT INTO beetle_images
                                (beetle_code, file_name, mime_type, image_data)
                                VALUES (?, ?, ?, ?)
                                """,
                                [
                                    (
                                        active_code,
                                        image.name,
                                        image.type or "application/octet-stream",
                                        image.getvalue(),
                                    )
                                    for image in uploaded_images
                                ],
                            )
                            conn.commit()
                        st.session_state[image_upload_version_key] = (
                            image_upload_version + 1
                        )
                        st.session_state[image_upload_success_key] = True
                        st.rerun()

                    with get_connection() as conn:
                        image_rows = conn.execute(
                            """
                            SELECT id, file_name, mime_type, image_data, created_at
                            FROM beetle_images
                            WHERE beetle_code=?
                            ORDER BY id DESC
                            """,
                            (active_code,),
                        ).fetchall()

                    if not image_rows:
                        st.info("目前尚未上傳圖片。")
                    else:
                        st.markdown(f"目前共有 {len(image_rows)} 張圖片")
                        for image_row in image_rows:
                            image_col, delete_col = st.columns([4, 1])
                            image_col.image(
                                image_row["image_data"],
                                caption=image_row["file_name"],
                                width=260,
                            )
                            if delete_col.button(
                                "刪除圖片",
                                key=f"delete_image_{active_code}_{image_row['id']}",
                            ):
                                with get_connection() as conn:
                                    conn.execute(
                                        "DELETE FROM beetle_images WHERE id=? AND beetle_code=?",
                                        (image_row["id"], active_code),
                                    )
                                    conn.commit()
                                st.rerun()

                # 6. 刪除個體
                elif st.session_state.get("current_action") == "delete":
                    st.error(
                        f"確定要刪除個體 {active_code} 及其所有履歷資料嗎？"
                    )
                    if st.button("確認刪除！", type="primary"):
                        with get_connection() as conn:
                            cursor = conn.cursor()
                            cursor.execute(
                                "DELETE FROM beetles WHERE beetle_code = ?",
                                (active_code,),
                            )
                            cursor.execute(
                                "DELETE FROM logs WHERE beetle_code = ?",
                                (active_code,),
                            )
                            cursor.execute(
                                "DELETE FROM beetle_images WHERE beetle_code = ?",
                                (active_code,),
                            )
                            conn.commit()
                        st.session_state.current_action = None
                        st.session_state.edit_target_code = None
                        st.success("刪除成功！")
                        st.rerun()

            if st.button("關閉操作視窗"):
                st.session_state.current_action = None
                st.session_state.edit_target_code = None
                st.rerun()

        if st.session_state.get("current_action") and st.session_state.get("edit_target_code"):
            render_action_dialog()


# ==========================================
# 頁面 3: 新增個體與成長紀錄
# ==========================================
elif menu == "新增個體與成長紀錄":
    st.title("新增個體與成長紀錄")

    create_form_keys = [
        "create_beetle_code",
        "create_custom_id",
        "create_species",
        "create_gender",
        "create_origin",
        "create_initial_stage",
        "create_hatch_date",
        "create_acquisition_source",
        "create_parents_info",
        "create_generation",
        "create_lineage",
        "create_father_id",
        "create_mother_id",
        "create_maintenance_days",
        "create_notes",
        "create_log_date",
        "create_log_length",
        "create_log_weight",
        "create_log_maintenance",
        "create_log_notes",
    ]
    if st.session_state.pop("clear_create_form", False):
        for form_key in create_form_keys:
            st.session_state.pop(form_key, None)

    with st.form("create_beetle_full_form", clear_on_submit=True):
        st.subheader("1. 建立個體基本檔案")

        col1, col2, col3 = st.columns(3)
        beetle_code = col1.text_input(
            "個體編號 (必填)", placeholder="例如: 2026-DHH-05", key="create_beetle_code"
        )
        custom_id = col2.text_input(
            "ID (必填)", placeholder="例如: DHH-M05", key="create_custom_id"
        )
        species = col3.text_input(
            "物種名稱 (必填)",
            placeholder="例如: 赫克力士長角大カブト",
            key="create_species",
        )

        col4, col5, col6, col7 = st.columns(4)
        gender = col4.selectbox(
            "性別", ["未確定", "公", "母"], key="create_gender"
        )
        origin = col5.text_input(
            "產地 (選填)", placeholder="如: 瓜地馬拉", key="create_origin"
        )
        initial_stage = col6.selectbox(
            "初始階段 (選填)",
            ["卵", "一齡幼蟲", "二齡幼蟲", "三齡幼蟲", "前蛹", "蛹", "成蟲"],
            key="create_initial_stage",
        )
        hatch_date = col7.date_input(
            "孵化/採收日期 (選填)", date.today(), key="create_hatch_date"
        )

        acquisition_source = st.text_input(
            "取得來源 (選填)",
            placeholder="如: 自繁、購入、交換、他人贈送",
            key="create_acquisition_source",
        )

        col8, col9, col10 = st.columns(3)
        parents_info = col8.text_input(
            "親代 (選填)",
            placeholder="如: 170mm極太系 x 75mm",
            key="create_parents_info",
        )
        generation = col9.text_input(
            "累代 (選填)", placeholder="如: CBF1", key="create_generation"
        )
        lineage = col10.text_input(
            "血統 (選填)", placeholder="如: 極太血統", key="create_lineage"
        )

        col11, col12, col13 = st.columns(3)
        father_id = col11.text_input("父本 ID (選填)", key="create_father_id")
        mother_id = col12.text_input("母本 ID (選填)", key="create_mother_id")
        m_days = col13.number_input(
            "個體換土提醒週期 (天)",
            value=60,
            step=5,
            key="create_maintenance_days",
        )

        notes = st.text_area("備註 (選填)", key="create_notes")

        st.markdown("---")
        st.subheader("2. 初始成長紀錄 (可填寫日期、體長、體重)")

        lc1, lc2, lc3, lc4 = st.columns([2, 2, 2, 3])
        ldate = lc1.date_input("紀錄日期", date.today(), key="create_log_date")
        llength = lc2.number_input(
            "體長 (mm)", min_value=0.0, step=0.1, key="create_log_length"
        )
        lweight = lc3.number_input(
            "體重 (g)", min_value=0.0, step=0.1, key="create_log_weight"
        )
        lmaintenance = lc4.checkbox(
            "本次為換土/維護紀錄", key="create_log_maintenance"
        )
        lnotes = lc4.text_input(
            "紀錄備註", placeholder="耗材/容器/狀態...", key="create_log_notes"
        )

        submit_btn = st.form_submit_button(
            "儲存並建立個體檔案", type="primary"
        )

    if submit_btn:
        if not beetle_code or not custom_id or not species:
            st.error("「個體編號」、「ID」與「物種名稱」為必填欄位！")
        else:
            try:
                with get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        INSERT INTO beetles (
                            beetle_code, custom_id, species, gender, origin, acquisition_source, initial_stage, 
                            current_stage, hatch_date, parents_info, generation, lineage, 
                            father_id, mother_id, notes, custom_maintenance_days
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            beetle_code,
                            custom_id,
                            species,
                            gender,
                            origin,
                            acquisition_source,
                            initial_stage,
                            initial_stage,
                            hatch_date.strftime("%Y-%m-%d"),
                            parents_info,
                            generation,
                            lineage,
                            father_id,
                            mother_id,
                            notes,
                            m_days,
                        ),
                    )

                    if llength > 0 or lweight > 0 or lnotes or lmaintenance:
                        cursor.execute(
                            """
                            INSERT INTO logs (beetle_code, entry_date, length_mm, weight_g, notes, maintenance_type)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """,
                            (
                                beetle_code,
                                ldate.strftime("%Y-%m-%d"),
                                llength if llength > 0 else None,
                                lweight if lweight > 0 else None,
                                lnotes,
                                "維護" if lmaintenance else "一般紀錄",
                            ),
                        )
                    conn.commit()

                st.success(f"成功建立個體與歷史紀錄：{beetle_code}")
                st.session_state.clear_create_form = True
                st.rerun()
            except sqlite3.IntegrityError:
                st.error(
                    f"個體編號 `{beetle_code}` 已存在，請檢查並更換編號！"
                )

# ==========================================
# 頁面 4: QR Code 掃描與識別
# ==========================================
elif menu == "QR Code 掃描與識別":
    st.title("QR Code 掃描與個體識別")
    st.caption(
        "使用手機鏡頭拍照、上傳瓶身 QR Code 照片，或輸入內容解碼個體檔案。"
    )

    scan_tab1, scan_tab2 = st.tabs(
        ["圖片上傳 / 照相掃描", "貼上 QR Code 文字數據"]
    )

    with scan_tab1:
        img_file = st.file_uploader(
            "請上傳 QR Code 標籤圖片", type=["png", "jpg", "jpeg"]
        )
        if img_file is not None:
            image = Image.open(img_file)
            st.image(image, caption="已上傳圖片", width=250)
            st.info(
                "提示：上傳影像成功！點擊下方 Tab 貼上解碼文字即可查詢資料。"
            )

    with scan_tab2:
        qr_text_input = st.text_area(
            "請貼上掃描條碼後讀取到的內容 (JSON 格式)：",
            placeholder='{"beetle_code": "2026-DHH-01", ...}',
        )
        if st.button("解碼與查詢個體檔案"):
            try:
                data = json.loads(qr_text_input)
                st.success(
                    f"解碼成功！個體編號：{data.get('beetle_code')}"
                )
                st.markdown("#### QR Code 完整資料")
                st.json(data)

                with get_connection() as conn:
                    df_beetles = pd.read_sql_query(
                        "SELECT * FROM beetles WHERE beetle_code = ?",
                        conn,
                        params=(data.get("beetle_code"),),
                    )

                if not df_beetles.empty:
                    info = df_beetles.iloc[0]
                    st.markdown("#### 資料庫目前資料")
                    st.json(dict(info))
                else:
                    st.warning("資料庫中查無該個體編號之紀錄。")
            except Exception:
                st.error("QR Code 數據無效或非對應 JSON 格式。")

# ==========================================
# 頁面 5: 通知管理
# ==========================================
elif menu == "通知管理":
    st.title("通知管理")
    st.caption("設定待換土/維護通知的寄件服務與收件信箱。")

    with get_connection() as conn:
        settings_row = conn.execute(
            "SELECT * FROM notification_settings WHERE id = 1"
        ).fetchone()
        recipient_rows = conn.execute(
            "SELECT slot, email, enabled FROM notification_recipients ORDER BY slot"
        ).fetchall()

    existing_recipient_count = max(
        [row["slot"] for row in recipient_rows if row["email"]], default=1
    )
    if "notification_recipient_count" not in st.session_state:
        st.session_state.notification_recipient_count = min(
            existing_recipient_count, 10
        )
    recipient_count = st.session_state.notification_recipient_count

    with st.form("notification_settings_form"):
        st.subheader("通知設定")
        enabled = st.checkbox(
            "啟用通知設定",
            value=bool(settings_row["enabled"]),
        )
        notification_days = st.number_input(
            "通知週期 (天)",
            min_value=1,
            max_value=365,
            value=int(settings_row["notification_days"]),
            step=1,
        )
        subject = st.text_input(
            "通知主旨",
            value=settings_row["subject"] or "甲蟲換土/維護提醒",
        )

        st.subheader("SMTP 寄件設定")
        smtp_host = st.text_input("SMTP 主機", value=settings_row["smtp_host"])
        smtp_port = st.number_input(
            "SMTP Port",
            min_value=1,
            max_value=65535,
            value=int(settings_row["smtp_port"]),
            step=1,
        )
        smtp_ssl = st.checkbox(
            "使用 SSL 連線",
            value=bool(settings_row["smtp_ssl"]),
        )
        smtp_username = st.text_input(
            "SMTP 帳號", value=settings_row["smtp_username"]
        )
        smtp_password = st.text_input(
            "SMTP 密碼", value=settings_row["smtp_password"], type="password"
        )
        sender_email = st.text_input(
            "寄件人信箱",
            value=settings_row["sender_email"] or settings_row["smtp_username"],
        )

        st.subheader("收件信箱 (最多 10 組)")
        recipient_values = []
        for row_index in range(recipient_count):
            recipient = recipient_rows[row_index]
            recipient_col1, recipient_col2 = st.columns([4, 1])
            recipient_email = recipient_col1.text_input(
                f"信箱 {row_index + 1}",
                value=recipient["email"] or "",
                key=f"notification_email_{row_index + 1}",
            )
            recipient_enabled = recipient_col2.checkbox(
                "啟用",
                value=bool(recipient["enabled"]),
                key=f"notification_enabled_{row_index + 1}",
            )
            recipient_values.append((row_index + 1, recipient_email, recipient_enabled))

        recipient_action_col1, recipient_action_col2, recipient_action_col3 = st.columns(
            [1, 1, 2]
        )
        add_recipient = recipient_action_col1.form_submit_button("新增信箱")
        remove_recipient = recipient_action_col2.form_submit_button("移除信箱")
        save_settings = recipient_action_col3.form_submit_button(
            "儲存通知設定", type="primary"
        )

    if add_recipient:
        if recipient_count >= 10:
            st.warning("最多只能設定 10 組收件信箱。")
        else:
            st.session_state.notification_recipient_count = recipient_count + 1
            st.rerun()

    if remove_recipient:
        if recipient_count <= 1:
            st.warning("至少保留 1 組收件信箱欄位。")
        else:
            removed_slot = recipient_count
            with get_connection() as conn:
                conn.execute(
                    "UPDATE notification_recipients SET email='', enabled=0 WHERE slot=?",
                    (removed_slot,),
                )
                conn.commit()
            st.session_state.pop(f"notification_email_{removed_slot}", None)
            st.session_state.pop(f"notification_enabled_{removed_slot}", None)
            st.session_state.notification_recipient_count = recipient_count - 1
            st.rerun()

    if save_settings:
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE notification_settings
                SET enabled=?, notification_days=?, smtp_host=?, smtp_port=?,
                    smtp_ssl=?, smtp_username=?, smtp_password=?, sender_email=?, subject=?
                WHERE id=1
                """,
                (
                    int(enabled),
                    int(notification_days),
                    smtp_host.strip(),
                    int(smtp_port),
                    int(smtp_ssl),
                    smtp_username.strip(),
                    smtp_password,
                    sender_email.strip(),
                    subject.strip() or "甲蟲換土/維護提醒",
                ),
            )
            conn.executemany(
                "UPDATE notification_recipients SET email=?, enabled=? WHERE slot=?",
                [
                    (email.strip(), int(slot_enabled), slot)
                    for slot, email, slot_enabled in recipient_values
                ]
                + [
                    ("", 0, slot)
                    for slot in range(recipient_count + 1, 11)
                ],
            )
            conn.commit()
        st.success("通知設定已儲存。")

    st.markdown("---")
    st.subheader("立即寄送測試通知")
    st.caption("按下按鈕後會依目前儲存的設定寄送，不會自動在背景執行。")
    if st.button("立即寄送通知"):
        with get_connection() as conn:
            current_settings = conn.execute(
                "SELECT * FROM notification_settings WHERE id = 1"
            ).fetchone()
            current_recipients = conn.execute(
                "SELECT email FROM notification_recipients WHERE enabled=1 AND TRIM(email) != ''"
            ).fetchall()

        recipients = [row["email"] for row in current_recipients]
        pending_records = get_pending_maintenance_records()
        if not current_settings["enabled"]:
            st.warning("通知功能尚未啟用，請先儲存並啟用通知設定。")
        elif not recipients:
            st.warning("尚未設定任何啟用中的收件信箱。")
        elif not current_settings["smtp_host"]:
            st.warning("尚未設定 SMTP 主機。")
        elif not pending_records:
            st.info("目前沒有達到換土/維護條件的個體，不寄送通知。")
        else:
            try:
                send_notification_email(
                    current_settings, recipients, pending_records
                )
                with get_connection() as conn:
                    conn.execute(
                        "UPDATE notification_settings SET last_sent_at=? WHERE id=1",
                        (datetime.now().isoformat(timespec="seconds"),),
                    )
                    conn.commit()
                st.success(f"通知已寄送至 {len(recipients)} 組信箱。")
            except Exception as ex:
                st.error(f"寄送失敗：{ex}")

# ==========================================
# 頁面 6: 備份/匯入
# ==========================================
elif menu == "備份/匯入":
    st.title("備份/匯入")
    st.caption("備份或還原系統中的個體、成長紀錄與通知設定。")

    import_success = st.session_state.pop("backup_import_success", False)
    if import_success:
        st.session_state.pop("backup_import_file", None)
        st.session_state.pop("backup_import_confirm", None)
        st.success("完整資料匯入成功，預覽資料已清空。")

    st.subheader("完整資料備份")
    backup_payload = create_backup_payload()
    backup_bytes = json.dumps(
        backup_payload, ensure_ascii=False, indent=2, default=str
    ).encode("utf-8")
    st.download_button(
        "下載完整備份 JSON",
        data=backup_bytes,
        file_name=f"beetle_tracker_backup_{date.today().isoformat()}.json",
        mime="application/json",
        use_container_width=True,
    )
    st.caption(
        "備份內容包含個體資料、成長紀錄、通知設定與最多 10 組收件信箱。"
    )

    st.markdown("---")
    st.subheader("完整資料匯入")
    uploaded_backup = st.file_uploader(
        "選擇備份 JSON 檔案",
        type=["json"],
        accept_multiple_files=False,
        key="backup_import_file",
    )

    import_payload = None
    if uploaded_backup is not None:
        try:
            import_payload = json.loads(uploaded_backup.getvalue().decode("utf-8"))
            if import_payload.get("format") != "beetle_tracker_backup":
                raise ValueError("檔案不是本系統產生的備份格式。")
            st.success("備份檔案格式正確。")
            table_summary = {
                table_name: len(import_payload.get("tables", {}).get(table_name, []))
                for table_name in BACKUP_TABLES
            }
            st.json(table_summary)
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, ValueError) as ex:
            st.error(f"備份檔案無法讀取：{ex}")
            import_payload = None

    confirm_import = st.checkbox(
        "我確認匯入後會覆寫目前系統中的全部資料。",
        disabled=import_payload is None,
        key="backup_import_confirm",
    )
    if st.button(
        "覆寫並匯入完整資料",
        type="primary",
        disabled=import_payload is None or not confirm_import,
        use_container_width=True,
        key="backup_import_submit",
    ):
        try:
            restore_backup_payload(import_payload)
            st.session_state.backup_import_success = True
            st.rerun()
        except Exception as ex:
            st.error(f"匯入失敗，原資料未變更：{ex}")