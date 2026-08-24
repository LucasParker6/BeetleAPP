import io
import json
import base64
import smtplib
from datetime import date, datetime
from email.message import EmailMessage
import pandas as pd
from PIL import Image
import qrcode
import streamlit as st
import altair as alt
from supabase import create_client, Client
import cv2
import numpy as np

# ==========================================
# 1. Supabase 連線初始化[cite: 1]
# ==========================================
@st.cache_resource
def init_supabase() -> Client:
    url = st.secrets["supabase"]["SUPABASE_URL"]
    key = st.secrets["supabase"]["SUPABASE_KEY"]
    return create_client(url, key)

supabase = init_supabase()

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

# ==========================================
# 2. Supabase 資料庫操作與備份機制[cite: 1]
# ==========================================
def create_backup_payload():
    """建立包含所有系統資料的 JSON 備份內容。"""
    payload = {
        "format": "beetle_tracker_backup",
        "version": 1,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "tables": {},
    }
    for table_name in BACKUP_TABLES:
        res = supabase.table(table_name).select("*").execute()
        records = res.data if res.data else []
        payload["tables"][table_name] = records
    return payload


def restore_backup_payload(payload):
    """驗證並還原完整 JSON 備份至 Supabase。"""
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

    try:
        for table_name in ["beetle_images", "logs", "notification_recipients", "notification_settings", "beetles"]:
            supabase.table(table_name).delete().neq("id" if table_name in ["logs", "beetle_images", "notification_settings"] else ("slot" if table_name == "notification_recipients" else "beetle_code"), "___dummy___").execute()
        
        for table_name in BACKUP_TABLES:
            records = payload["tables"].get(table_name, [])
            if records:
                if table_name in ["logs", "beetle_images"]:
                    for r in records:
                        r.pop("id", None)
                supabase.table(table_name).insert(records).execute()
    except Exception as ex:
        raise ValueError(f"還原時發生錯誤：{ex}")


def get_pending_maintenance_records():
    """取得目前需要換土或維護的個體清單。"""
    today = date.today()
    res_b = supabase.table("beetles").select("*").execute()
    res_l = supabase.table("logs").select("*").execute()
    
    df_beetles = pd.DataFrame(res_b.data if res_b.data else [])
    df_logs = pd.DataFrame(res_l.data if res_l.data else [])

    if df_beetles.empty or "beetle_code" not in df_beetles.columns:
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
        if not maintenance_logs.empty and "maintenance_type" in maintenance_logs.columns:
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
                        last_length if pd.notnull(last_length) and last_length is not None else "-"
                    ),
                    "最新體重 (g)": (
                        last_weight if pd.notnull(last_weight) and last_weight is not None else "-"
                    ),
                }
            )
    return pending_list


def send_notification_email(settings, recipients, pending_list):
    """透過 SMTP 寄送待換土通知。"""
    message = EmailMessage()
    message["Subject"] = settings.get("subject") or "甲蟲換土/維護提醒"
    message["From"] = settings.get("sender_email") or settings.get("smtp_username")
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

    if settings.get("smtp_ssl"):
        with smtplib.SMTP_SSL(
            settings["smtp_host"], int(settings["smtp_port"]), timeout=20
        ) as smtp:
            if settings.get("smtp_username"):
                smtp.login(settings["smtp_username"], settings["smtp_password"])
            smtp.send_message(message)
    else:
        with smtplib.SMTP(
            settings["smtp_host"], int(settings["smtp_port"]), timeout=20
        ) as smtp:
            smtp.starttls()
            if settings.get("smtp_username"):
                smtp.login(settings["smtp_username"], settings["smtp_password"])
            smtp.send_message(message)


def init_db():
    """初始化 Supabase 基礎設定"""
    res = supabase.table("notification_settings").select("*").eq("id", 1).execute()
    if not res.data:
        supabase.table("notification_settings").insert({
            "id": 1,
            "enabled": 0,
            "notification_days": 1,
            "smtp_host": "",
            "smtp_port": 587,
            "smtp_ssl": 0,
            "smtp_username": "",
            "smtp_password": "",
            "sender_email": "",
            "subject": "甲蟲換土/維護提醒"
        }).execute()
    
    res_rec = supabase.table("notification_recipients").select("slot").execute()
    existing_slots = [r["slot"] for r in res_rec.data] if res_rec.data else []
    missing_slots = [{"slot": s, "email": "", "enabled": 1} for s in range(1, 11) if s not in existing_slots]
    if missing_slots:
        supabase.table("notification_recipients").insert(missing_slots).execute()


# ==========================================
# 3. QR Code 生成工具 (易讀繁體中文 JSON 格式)[cite: 1]
# ==========================================
def generate_qrcode(beetle_data: dict) -> Image.Image:
    """生成包含易讀格式個體資料與歷史紀錄的 QR Code 圖檔"""
    qr_json = json.dumps(beetle_data, ensure_ascii=False, indent=2, default=str)
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
# 4. Streamlit 主程式介面[cite: 1]
# ==========================================
st.set_page_config(
    page_title="甲蟲專業飼育紀錄系統",
    layout="wide",
)

init_db()

# 初始化 Session State[cite: 1]
if "edit_target_code" not in st.session_state:
    st.session_state.edit_target_code = None
if "current_action" not in st.session_state:
    st.session_state.current_action = None
if "edit_log_rows" not in st.session_state:
    st.session_state.edit_log_rows = 1

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
    default="QR Code 掃描與識別",
    key="main_menu",
    on_change=clear_action_on_menu_change,
)

# ==========================================
# 頁面 1: 全場總覽與待換土提醒[cite: 1]
# ==========================================
if menu == "全場總覽與待換土提醒":
    st.title("全場總覽與待換土提醒")

    res_b = supabase.table("beetles").select("*").execute()
    res_l = supabase.table("logs").select("*").execute()
    
    df_beetles = pd.DataFrame(res_b.data if res_b.data else [])
    df_logs = pd.DataFrame(res_l.data if res_l.data else [])

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
            if not maintenance_logs.empty and "maintenance_type" in maintenance_logs.columns:
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
                            last_length if pd.notnull(last_length) and last_length is not None else "-"
                        ),
                        "最新體重 (g)": (
                            last_weight if pd.notnull(last_weight) and last_weight is not None else "-"
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
            "目前資料庫中沒有任何活體甲蟲檔案，請前往「新增個體」分頁建立。"
        )
    else:
        st.success("全場狀況良好，目前沒有到達換土週期的個體！")

# ==========================================
# 頁面 2: 個體清單與檔案管理[cite: 1]
# ==========================================
elif menu == "個體清單與檔案管理":
    st.title("個體清單與檔案管理")

    res_b = supabase.table("beetles").select("*").execute()
    res_l = supabase.table("logs").select("*").execute()

    df_beetles = pd.DataFrame(res_b.data if res_b.data else [])
    df_logs = pd.DataFrame(res_l.data if res_l.data else [])

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
            active_code = st.session_state.get("edit_target_code")
            matching_beetles = df_valid[df_valid["beetle_code"] == active_code]

            if not matching_beetles.empty:
                selected_info = matching_beetles.iloc[0]

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

                elif st.session_state.get("current_action") == "edit":
                    st.subheader(f"編輯個體資料：{active_code}")

                    b_logs_list = []
                    if not df_logs.empty and "beetle_code" in df_logs.columns:
                        b_logs_list = (
                            df_logs[df_logs["beetle_code"] == active_code]
                            .sort_values("entry_date")
                            .to_dict("records")
                        )

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

                        if btn_add_log:
                            st.session_state.edit_log_rows += 1
                            st.rerun()

                        if btn_del_log:
                            if st.session_state.edit_log_rows > 1:
                                st.session_state.edit_log_rows -= 1
                                st.rerun()

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
                                    update_payload = {
                                        "beetle_code": edit_beetle_code,
                                        "custom_id": edit_custom_id,
                                        "species": edit_species,
                                        "gender": edit_gender,
                                        "origin": edit_origin,
                                        "acquisition_source": edit_acquisition_source,
                                        "initial_stage": edit_initial_stage,
                                        "current_stage": edit_stage,
                                        "hatch_date": edit_hatch_date.strftime("%Y-%m-%d"),
                                        "parents_info": edit_parents_info,
                                        "generation": edit_generation,
                                        "lineage": edit_lineage,
                                        "father_id": edit_father_id,
                                        "mother_id": edit_mother_id,
                                        "notes": edit_notes,
                                        "custom_maintenance_days": edit_m_days,
                                    }
                                    
                                    supabase.table("beetles").update(update_payload).eq("beetle_code", active_code).execute()

                                    supabase.table("logs").delete().eq("beetle_code", active_code).execute()
                                    if edit_beetle_code != active_code:
                                        supabase.table("logs").delete().eq("beetle_code", edit_beetle_code).execute()

                                    logs_to_insert = []
                                    for elog in edited_logs:
                                        if (
                                            elog["length"]
                                            or elog["weight"]
                                            or elog["notes"]
                                        ):
                                            logs_to_insert.append({
                                                "beetle_code": edit_beetle_code,
                                                "entry_date": elog["date"],
                                                "length_mm": elog["length"],
                                                "weight_g": elog["weight"],
                                                "notes": elog["notes"],
                                                "maintenance_type": "維護" if elog["maintenance"] else "一般紀錄",
                                            })
                                    if logs_to_insert:
                                        supabase.table("logs").insert(logs_to_insert).execute()

                                    st.session_state.edit_target_code = edit_beetle_code
                                    st.session_state.current_action = "view"
                                    st.success("資料與成長紀錄修改成功並已儲存！")
                                    st.rerun()

                                except Exception as ex:
                                    st.error(f"資料庫更新發生錯誤：{ex}")

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

                elif st.session_state.get("current_action") == "qr":
                    st.subheader(f"{active_code} 專屬 QR Code")
                    
                    res_logs = supabase.table("logs").select("entry_date, length_mm, weight_g, maintenance_type, notes").eq("beetle_code", active_code).order("entry_date").execute()
                    logs_data = res_logs.data if res_logs.data else []

                    formatted_logs = []
                    for log in logs_data:
                        log_item = {"日期": log.get("entry_date")}
                        if log.get("length_mm") is not None:
                            log_item["體長(mm)"] = log.get("length_mm")
                        if log.get("weight_g") is not None:
                            log_item["體重(g)"] = log.get("weight_g")
                        log_item["類型"] = log.get("maintenance_type", "紀錄")
                        if log.get("notes"):
                            log_item["備註"] = log.get("notes")
                        formatted_logs.append(log_item)

                    qr_payload = {
                        "個體編號": selected_info.get("beetle_code"),
                        "物種名稱": selected_info.get("species"),
                        "性別": selected_info.get("gender"),
                        "當前階段": selected_info.get("current_stage"),
                        "成長紀錄": formatted_logs
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
                        new_imgs = []
                        for img in uploaded_images:
                            b64 = base64.b64encode(img.getvalue()).decode("ascii")
                            new_imgs.append({
                                "beetle_code": active_code,
                                "file_name": img.name,
                                "mime_type": img.type or "application/octet-stream",
                                "image_data": b64
                            })
                        supabase.table("beetle_images").insert(new_imgs).execute()
                        st.session_state[image_upload_version_key] = (
                            image_upload_version + 1
                        )
                        st.session_state[image_upload_success_key] = True
                        st.rerun()

                    res_img = supabase.table("beetle_images").select("*").eq("beetle_code", active_code).order("id", desc=True).execute()
                    image_rows = res_img.data if res_img.data else []

                    if not image_rows:
                        st.info("目前尚未上傳圖片。")
                    else:
                        st.markdown(f"目前共有 {len(image_rows)} 張圖片")
                        for image_row in image_rows:
                            image_col, delete_col = st.columns([4, 1])
                            try:
                                img_bytes = base64.b64decode(image_row["image_data"])
                            except Exception:
                                img_bytes = image_row["image_data"]
                                
                            image_col.image(
                                img_bytes,
                                caption=image_row["file_name"],
                                width=260,
                            )
                            if delete_col.button(
                                "刪除圖片",
                                key=f"delete_image_{active_code}_{image_row['id']}",
                            ):
                                supabase.table("beetle_images").delete().eq("id", image_row["id"]).execute()
                                st.rerun()

                elif st.session_state.get("current_action") == "delete":
                    st.error(
                        f"確定要刪除個體 {active_code} 及其所有履歷資料嗎？"
                    )
                    if st.button("確認刪除！", type="primary"):
                        supabase.table("beetles").delete().eq("beetle_code", active_code).execute()
                        supabase.table("logs").delete().eq("beetle_code", active_code).execute()
                        supabase.table("beetle_images").delete().eq("beetle_code", active_code).execute()
                        
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
# 頁面 3: 新增個體與成長紀錄[cite: 1]
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
                beetle_payload = {
                    "beetle_code": beetle_code,
                    "custom_id": custom_id,
                    "species": species,
                    "gender": gender,
                    "origin": origin,
                    "acquisition_source": acquisition_source,
                    "initial_stage": initial_stage,
                    "current_stage": initial_stage,
                    "hatch_date": hatch_date.strftime("%Y-%m-%d"),
                    "parents_info": parents_info,
                    "generation": generation,
                    "lineage": lineage,
                    "father_id": father_id,
                    "mother_id": mother_id,
                    "notes": notes,
                    "custom_maintenance_days": m_days,
                }
                supabase.table("beetles").insert(beetle_payload).execute()

                if llength > 0 or lweight > 0 or lnotes or lmaintenance:
                    log_payload = {
                        "beetle_code": beetle_code,
                        "entry_date": ldate.strftime("%Y-%m-%d"),
                        "length_mm": llength if llength > 0 else None,
                        "weight_g": lweight if lweight > 0 else None,
                        "notes": lnotes,
                        "maintenance_type": "維護" if lmaintenance else "一般紀錄",
                    }
                    supabase.table("logs").insert(log_payload).execute()

                st.success(f"成功建立個體與歷史紀錄：{beetle_code}")
                st.session_state.clear_create_form = True
                st.rerun()
            except Exception as ex:
                st.error(f"建立失敗，個體編號 `{beetle_code}` 可能已存在或發生錯誤：{ex}")

# ==========================================
# 頁面 4: QR Code 掃描與識別 (使用 OpenCV 進行解碼)[cite: 1]
# ==========================================
elif menu == "QR Code 掃描與識別":
    st.title("QR Code 掃描與個體識別")
    st.caption("使用手機/電腦鏡頭拍照、上傳瓶身照片，或輸入內容解碼個體檔案。")

    scan_tab1, scan_tab2, scan_tab3 = st.tabs([
        "📷 相機即時拍照", 
        "📁 圖片上傳掃描", 
        "📝 貼上 QR Code 文字數據"
    ])

    decoded_json_str = None

    def process_qr_image(img_input):
        """解析圖檔並使用 OpenCV 自動提取 QR Code 內容"""
        image = Image.open(img_input)
        st.image(image, caption="待掃描影像", width=300)
        
        # 將 PIL Image 轉換為 OpenCV 格式 (BGR)
        img_np = np.array(image.convert("RGB"))
        img_cv = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)

        # 使用 OpenCV 進行 QR Code 辨識
        detector = cv2.QRCodeDetector()
        qr_content, bbox, _ = detector.detectAndDecode(img_cv)

        if qr_content:
            st.success("🎉 自動辨識解碼成功！")
            return qr_content
        else:
            st.error("❌ 照片中未偵測到有效的 QR Code，請確認照片對焦清晰並重試。")
            return None

    # Tab 1: 相機拍照
    with scan_tab1:
        st.markdown("#### 拍照辨識")
        camera_img = st.camera_input("請將鏡頭對準標籤 QR Code 並按下拍照", key="qr_camera_input")
        if camera_img is not None:
            decoded_json_str = process_qr_image(camera_img)

    # Tab 2: 圖片上傳
    with scan_tab2:
        st.markdown("#### 檔案上傳")
        img_file = st.file_uploader("請上傳 QR Code 標籤圖片", type=["png", "jpg", "jpeg", "webp"], key="qr_file_uploader")
        if img_file is not None:
            decoded_json_str = process_qr_image(img_file)

    # Tab 3: 文字貼上
    with scan_tab3:
        st.markdown("#### 手動貼上內容")
        manual_text = st.text_area(
            "請貼上條碼讀取到的 JSON 數據：",
            placeholder='{\n  "個體編號": "DHH-202607-003",\n  "物種名稱": "長戟大兜蟲DHH"\n}',
            height=180,
            key="qr_manual_input"
        )
        if st.button("解析輸入內容", type="primary"):
            decoded_json_str = manual_text

    # 解碼與資料庫同步展示
    if decoded_json_str:
        st.markdown("---")
        try:
            data = json.loads(decoded_json_str)
            
            b_code = data.get("個體編號") or data.get("beetle_code")
            species = data.get("物種名稱") or data.get("species")
            gender = data.get("性別") or data.get("gender")
            stage = data.get("當前階段") or data.get("current_stage")
            logs = data.get("成長紀錄") or data.get("logs") or []

            st.subheader(f"📌 個體檔案資訊：{b_code or '未命名'}")
            
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("個體編號", b_code or "-")
            c2.metric("物種名稱", species or "-")
            c3.metric("性別", gender or "-")
            c4.metric("當前階段", stage or "-")

            st.markdown("##### 📜 標籤履歷紀錄")
            if logs:
                st.dataframe(pd.DataFrame(logs), use_container_width=True)
            else:
                st.caption("此標籤未包含歷史紀錄數據。")

            if b_code:
                st.markdown("---")
                res = supabase.table("beetles").select("*").eq("beetle_code", b_code).execute()
                if res.data:
                    st.markdown("#### 🔄 資料庫最新即時狀態")
                    st.json(res.data[0])
                else:
                    st.warning("⚠️ 資料庫中查無此個體編號的最新紀錄（可能已被刪除或為離線標籤）。")
        
        except Exception as e:
            st.error(f"❌ 解析失敗：內容格式不正確或資料損壞。({e})")

# ==========================================
# 頁面 5: 通知管理[cite: 1]
# ==========================================
elif menu == "通知管理":
    st.title("通知管理")
    st.caption("設定待換土/維護通知的寄件服務與收件信箱。")

    res_set = supabase.table("notification_settings").select("*").eq("id", 1).execute()
    settings_row = res_set.data[0] if res_set.data else {}

    res_rec = supabase.table("notification_recipients").select("slot, email, enabled").order("slot").execute()
    recipient_rows = res_rec.data if res_rec.data else []

    existing_recipient_count = max(
        [row["slot"] for row in recipient_rows if row.get("email")], default=1
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
            value=bool(settings_row.get("enabled", 0)),
        )
        notification_days = st.number_input(
            "通知週期 (天)",
            min_value=1,
            max_value=365,
            value=int(settings_row.get("notification_days", 1)),
            step=1,
        )
        subject = st.text_input(
            "通知主旨",
            value=settings_row.get("subject") or "甲蟲換土/維護提醒",
        )

        st.subheader("SMTP 寄件設定")
        smtp_host = st.text_input("SMTP 主機", value=settings_row.get("smtp_host", ""))
        smtp_port = st.number_input(
            "SMTP Port",
            min_value=1,
            max_value=65535,
            value=int(settings_row.get("smtp_port", 587)),
            step=1,
        )
        smtp_ssl = st.checkbox(
            "使用 SSL 連線",
            value=bool(settings_row.get("smtp_ssl", 0)),
        )
        smtp_username = st.text_input(
            "SMTP 帳號", value=settings_row.get("smtp_username", "")
        )
        smtp_password = st.text_input(
            "SMTP 密碼", value=settings_row.get("smtp_password", ""), type="password"
        )
        sender_email = st.text_input(
            "寄件人信箱",
            value=settings_row.get("sender_email") or settings_row.get("smtp_username", ""),
        )

        st.subheader("收件信箱 (最多 10 組)")
        recipient_values = []
        for row_index in range(recipient_count):
            recipient = recipient_rows[row_index] if row_index < len(recipient_rows) else {"email": "", "enabled": 1}
            recipient_col1, recipient_col2 = st.columns([4, 1])
            recipient_email = recipient_col1.text_input(
                f"信箱 {row_index + 1}",
                value=recipient.get("email") or "",
                key=f"notification_email_{row_index + 1}",
            )
            recipient_enabled = recipient_col2.checkbox(
                "啟用",
                value=bool(recipient.get("enabled", 1)),
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
            supabase.table("notification_recipients").update({"email": "", "enabled": 0}).eq("slot", removed_slot).execute()
            st.session_state.pop(f"notification_email_{removed_slot}", None)
            st.session_state.pop(f"notification_enabled_{removed_slot}", None)
            st.session_state.notification_recipient_count = recipient_count - 1
            st.rerun()

    if save_settings:
        settings_payload = {
            "enabled": int(enabled),
            "notification_days": int(notification_days),
            "smtp_host": smtp_host.strip(),
            "smtp_port": int(smtp_port),
            "smtp_ssl": int(smtp_ssl),
            "smtp_username": smtp_username.strip(),
            "smtp_password": smtp_password,
            "sender_email": sender_email.strip(),
            "subject": subject.strip() or "甲蟲換土/維護提醒",
        }
        supabase.table("notification_settings").update(settings_payload).eq("id", 1).execute()

        rec_upsert_payload = [
            {"slot": slot, "email": email.strip(), "enabled": int(slot_enabled)}
            for slot, email, slot_enabled in recipient_values
        ] + [
            {"slot": slot, "email": "", "enabled": 0}
            for slot in range(recipient_count + 1, 11)
        ]
        supabase.table("notification_recipients").upsert(rec_upsert_payload).execute()
        st.success("通知設定已儲存。")

    st.markdown("---")
    st.subheader("立即寄送測試通知")
    st.caption("按下按鈕後會依目前儲存的設定寄送，不會自動在背景執行。")
    if st.button("立即寄送通知"):
        res_set = supabase.table("notification_settings").select("*").eq("id", 1).execute()
        current_settings = res_set.data[0] if res_set.data else {}

        res_rec = supabase.table("notification_recipients").select("email").eq("enabled", 1).neq("email", "").execute()
        recipients = [row["email"] for row in res_rec.data if row.get("email", "").strip()]

        pending_records = get_pending_maintenance_records()
        if not current_settings.get("enabled"):
            st.warning("通知功能尚未啟用，請先儲存並啟用通知設定。")
        elif not recipients:
            st.warning("尚未設定任何啟用中的收件信箱。")
        elif not current_settings.get("smtp_host"):
            st.warning("尚未設定 SMTP 主機。")
        elif not pending_records:
            st.info("目前沒有達到換土/維護條件的個體，不寄送通知。")
        else:
            try:
                send_notification_email(
                    current_settings, recipients, pending_records
                )
                supabase.table("notification_settings").update({
                    "last_sent_at": datetime.now().isoformat(timespec="seconds")
                }).eq("id", 1).execute()
                st.success(f"通知已寄送至 {len(recipients)} 組信箱。")
            except Exception as ex:
                st.error(f"寄送失敗：{ex}")

# ==========================================
# 頁面 6: 備份/匯入[cite: 1]
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