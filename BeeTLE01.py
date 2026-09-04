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
from postgrest.exceptions import APIError
import cv2
import numpy as np
import graphviz

# ==========================================
# 1. Supabase 連線初始化
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
    "announcements",
    "breeding_rooms",
    "larvae_batches",
]
BACKUP_REQUIRED_TABLES = [
    table_name for table_name in BACKUP_TABLES if table_name != "beetle_images"
]

# ==========================================
# 2. Supabase 資料庫操作與備份機制
# ==========================================
def table_exists(table_name: str) -> bool:
    """檢查 Supabase 中指定資料表是否存在，避免未建立表格造成頁面直接崩潰。"""
    try:
        supabase.table(table_name).select("id").limit(1).execute()
        return True
    except Exception as exc:
        message = str(exc)
        if (
            "Could not find the table" in message
            or "PGRST205" in message
            or "does not exist" in message.lower()
            or "relation" in message.lower() and "does not exist" in message.lower()
        ):
            return False
        raise


def create_backup_payload():
    """建立包含所有系統資料的 JSON 備份內容。"""
    payload = {
        "format": "beetle_tracker_backup",
        "version": 1,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "tables": {},
    }
    for table_name in BACKUP_TABLES:
        if not table_exists(table_name):
            payload["tables"][table_name] = []
            continue
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
        for table_name in [
            "beetle_images",
            "logs",
            "notification_recipients",
            "notification_settings",
            "beetles",
            "announcements",
            "breeding_rooms",
            "larvae_batches",
        ]:
            if not table_exists(table_name):
                continue
            key_column = {
                "logs": "id",
                "beetle_images": "id",
                "notification_settings": "id",
                "announcements": "id",
                "breeding_rooms": "room_code",
                "larvae_batches": "batch_code",
                "notification_recipients": "slot",
                "beetles": "beetle_code",
            }.get(table_name, "id")
            supabase.table(table_name).delete().neq(key_column, "___dummy___").execute()

        for table_name in BACKUP_TABLES:
            if not table_exists(table_name):
                continue
            records = payload["tables"].get(table_name, [])
            if records:
                if table_name in ["logs", "beetle_images", "announcements", "breeding_rooms", "larvae_batches"]:
                    for r in records:
                        r.pop("id", None)
                supabase.table(table_name).insert(records).execute()
    except Exception as ex:
        raise ValueError(f"還原時發生錯誤：{ex}")


def get_pending_maintenance_records():
    """取得目前需要換土或維護的個體與幼蟲批次清單。"""
    today = date.today()
    res_b = supabase.table("beetles").select("*").execute() if table_exists("beetles") else None
    res_l = supabase.table("logs").select("*").execute() if table_exists("logs") else None
    res_larvae = supabase.table("larvae_batches").select("*").execute() if table_exists("larvae_batches") else None
    res_rooms = supabase.table("breeding_rooms").select("*").execute() if table_exists("breeding_rooms") else None

    df_beetles = pd.DataFrame(res_b.data if res_b and res_b.data else [])
    df_logs = pd.DataFrame(res_l.data if res_l and res_l.data else [])
    df_larvae = pd.DataFrame(res_larvae.data if res_larvae and res_larvae.data else [])
    df_rooms = pd.DataFrame(res_rooms.data if res_rooms and res_rooms.data else [])
    pending_list = []

    if not df_beetles.empty and "beetle_code" in df_beetles.columns:
        df_valid = df_beetles[
            df_beetles["beetle_code"].notna()
            & (df_beetles["beetle_code"].astype(str).str.strip() != "")
        ].copy()

        # 總列管數量只計算活體，明確排除 current_stage =「死亡」。
        # fillna + strip 可避免 NULL/空白資料造成篩選異常。
        df_active = df_valid.copy()
        if "current_stage" in df_active.columns:
            df_active = df_active[df_active["current_stage"].fillna("").astype(str).str.strip() != "死亡"]

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
                        "類型": "個體",
                    }
                )

    if not df_larvae.empty and "batch_code" in df_larvae.columns:
        for _, batch in df_larvae.iterrows():
            batch_code = batch.get("batch_code")
            if not batch_code:
                continue

            stage = batch.get("current_stage", "未設定")
            status = batch.get("status", "開始") or "開始"
            if status == "結束" or stage in ["蛹", "成蟲", "死亡"]:
                continue

            target_days = batch.get("maintenance_days")
            if pd.isna(target_days) or not target_days:
                target_days = 60
            target_days = int(target_days)

            harvest_date = batch.get("harvest_date")
            try:
                last_date = datetime.strptime(str(harvest_date), "%Y-%m-%d").date()
                days_passed = (today - last_date).days
            except (TypeError, ValueError):
                last_date = None
                days_passed = 999

            if days_passed >= target_days:
                pending_list.append(
                    {
                        "個體編號": batch_code,
                        "ID": batch.get("batch_code", "-"),
                        "物種": batch.get("species", "-"),
                        "當前階段": stage,
                        "個體提醒週期": f"{target_days} 天",
                        "上次換土日": (harvest_date or "尚未紀錄"),
                        "已相隔天數": (
                            f"{days_passed} 天"
                            if days_passed != 999
                            else "未曾紀錄"
                        ),
                        "最新體長 (mm)": "-",
                        "最新體重 (g)": "-",
                        "類型": "幼蟲批次",
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
    if not table_exists("notification_settings"):
        return

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

    if table_exists("notification_recipients"):
        res_rec = supabase.table("notification_recipients").select("slot").execute()
        existing_slots = [r["slot"] for r in res_rec.data] if res_rec.data else []
        missing_slots = [{"slot": s, "email": "", "enabled": 1} for s in range(1, 11) if s not in existing_slots]
        if missing_slots:
            supabase.table("notification_recipients").insert(missing_slots).execute()


# ==========================================
# 3. QR Code 生成工具 (易讀繁體中文 JSON 格式)
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
# 輔助函式：格式化父/母 ID 顯示
# ==========================================
def format_parent_display(father_id, mother_id):
    """
    格式化父/母 ID 顯示：
    - 若兩者皆有 ID，顯示格式如：父本ID / 母本ID
    - 若其中一方無 ID，給予相對應的文字替代
    - 若皆無，則顯示「無」
    """
    has_father_id = bool(father_id and str(father_id).strip())
    has_mother_id = bool(mother_id and str(mother_id).strip())
    
    if has_father_id and has_mother_id:
        return f"{father_id} / {mother_id}"
    elif has_father_id:
        return f"{father_id} / (無母本ID)"
    elif has_mother_id:
        return f"(無父本ID) / {mother_id}"
    else:
        return "無"


# ==========================================
# 4. 血統樹/族譜圖繪製核心邏輯 (Pedigree Tree Graph)
# ==========================================
def generate_pedigree_dot(target_code: str, depth: int = 3, show_offspring: bool = False) -> str:
    """從 Supabase 取得資料並動態生成 Graphviz DOT 語言字串"""
    res_b = supabase.table("beetles").select("*").execute()
    res_l = supabase.table("logs").select("*").execute()
    
    beetles = res_b.data if res_b.data else []
    logs = res_l.data if res_l.data else []
    
    beetle_map = {}
    id_to_code_map = {}
    for b in beetles:
        code = b.get("beetle_code")
        c_id = b.get("custom_id")
        if code:
            beetle_map[code] = b
            if c_id:
                id_to_code_map[str(c_id).strip()] = code

    length_map = {}
    if logs:
        df_logs = pd.DataFrame(logs)
        if "beetle_code" in df_logs.columns and "length_mm" in df_logs.columns:
            df_valid_len = df_logs.dropna(subset=["length_mm"]).sort_values("entry_date", ascending=False)
            for _, row in df_valid_len.iterrows():
                b_c = row["beetle_code"]
                if b_c not in length_map:
                    length_map[b_c] = row["length_mm"]

    def find_beetle_code(identifier):
        if not identifier:
            return None
        s_id = str(identifier).strip()
        if s_id in beetle_map:
            return s_id
        if s_id in id_to_code_map:
            return id_to_code_map[s_id]
        return None

    visited_nodes = set()
    nodes_dot = []
    edges_dot = []

    def get_node_style(b_info, is_target=False, is_unknown=False):
        if is_target:
            return 'shape=box, style="filled,rounded", fillcolor="#fef08a", color="#ca8a04", penwidth=2'
        if is_unknown:
            return 'shape=box, style="filled,dashed", fillcolor="#f3f4f6", color="#6b7280"'
        
        gender = b_info.get("gender", "未確定")
        if gender == "公":
            return 'shape=box, style="filled,rounded", fillcolor="#dbeafe", color="#1d4ed8"'
        elif gender == "母":
            return 'shape=box, style="filled,rounded", fillcolor="#fce7f3", color="#be185d", penwidth=2'
        else:
            return 'shape=box, style="filled,dashed", fillcolor="#f3f4f6", color="#6b7280"'

    def build_node_label(identifier, b_info=None, is_unknown=False):
        if is_unknown or not b_info:
            return f"未知 / 未登錄\\n({identifier})"
        cid = b_info.get("custom_id") or identifier
        species = b_info.get("species", "未知")
        lineage = b_info.get("lineage")
        code = b_info.get("beetle_code")
        len_val = length_map.get(code)
        
        f_id = b_info.get("father_id", "")
        m_id = b_info.get("mother_id", "")
        parent_str = format_parent_display(f_id, m_id)
        
        label_parts = [f"[ID] {cid}"]
        if species and species != "-":
            label_parts.append(f"物種: {species}")
        if lineage and lineage != "-":
            label_parts.append(f"血統: {lineage}")
        if parent_str != "無":
            label_parts.append(f"父/母: {parent_str}")
        if len_val:
            label_parts.append(f"體長: {len_val}mm")
        return "\\n".join(label_parts)

    def trace_ancestors(curr_code, curr_depth):
        resolved_code = find_beetle_code(curr_code)
        actual_code = resolved_code if resolved_code else curr_code
        
        if not actual_code or actual_code in visited_nodes:
            return
        visited_nodes.add(actual_code)

        b_info = beetle_map.get(actual_code)
        is_target = (actual_code == target_code)
        style = get_node_style(b_info, is_target=is_target, is_unknown=(b_info is None))
        label = build_node_label(actual_code, b_info, is_unknown=(b_info is None))
        
        safe_node_id = f"node_{hash(actual_code) & 0xffffffff}"
        nodes_dot.append(f'  {safe_node_id} [label="{label}", {style}];')

        if curr_depth >= depth or not b_info:
            return

        f_id = b_info.get("father_id")
        if f_id and str(f_id).strip():
            f_code = find_beetle_code(f_id) or str(f_id).strip()
            safe_f_id = f"node_{hash(f_code) & 0xffffffff}"
            edges_dot.append(f'  {safe_f_id} -> {safe_node_id} [label="父"];')
            trace_ancestors(f_code, curr_depth + 1)

        m_id = b_info.get("mother_id")
        if m_id and str(m_id).strip():
            m_code = find_beetle_code(m_id) or str(m_id).strip()
            safe_m_id = f"node_{hash(m_code) & 0xffffffff}"
            edges_dot.append(f'  {safe_m_id} -> {safe_node_id} [label="母"];')
            trace_ancestors(m_code, curr_depth + 1)

    trace_ancestors(target_code, 1)

    if show_offspring:
        target_info = beetle_map.get(target_code)
        target_cid = target_info.get("custom_id") if target_info else None
        
        for b_code, b_item in beetle_map.items():
            f_id = str(b_item.get("father_id", "")).strip()
            m_id = str(b_item.get("mother_id", "")).strip()
            
            is_child = False
            edge_label = "子"
            if f_id and (f_id == target_code or (target_cid and f_id == str(target_cid))):
                is_child = True
                edge_label = "父子"
            elif m_id and (m_id == target_code or (target_cid and m_id == str(target_cid))):
                is_child = True
                edge_label = "母子"

            if is_child and b_code not in visited_nodes:
                visited_nodes.add(b_code)
                style = get_node_style(b_item)
                label = build_node_label(b_code, b_item)
                
                safe_target_id = f"node_{hash(target_code) & 0xffffffff}"
                safe_child_id = f"node_{hash(b_code) & 0xffffffff}"
                
                nodes_dot.append(f'  {safe_child_id} [label="{label}", {style}];')
                edges_dot.append(f'  {safe_target_id} -> {safe_child_id} [label="{edge_label}"];')

    dot_code = "digraph PedigreeTree {\n"
    dot_code += "  rankdir=LR;\n"
    dot_code += '  node [fontname="Microsoft JhengHei"];\n'
    dot_code += "  " + "\n  ".join(nodes_dot) + "\n"
    dot_code += "  " + "\n  ".join(edges_dot) + "\n"
    dot_code += "}\n"
    
    return dot_code


# ==========================================
# 5. Streamlit 主程式介面
# ==========================================
st.set_page_config(
    page_title="甲蟲專業飼育紀錄系統",
    layout="wide",
)

init_db()

if "edit_target_code" not in st.session_state:
    st.session_state.edit_target_code = None
if "current_action" not in st.session_state:
    st.session_state.current_action = None
if "edit_log_rows" not in st.session_state:
    st.session_state.edit_log_rows = 1
if "announcement_action" not in st.session_state:
    st.session_state.announcement_action = None
if "target_announcement" not in st.session_state:
    st.session_state.target_announcement = None
if "breeding_room_action" not in st.session_state:
    st.session_state.breeding_room_action = None
if "breeding_room_edit_id" not in st.session_state:
    st.session_state.breeding_room_edit_id = None
if "larvae_batch_action" not in st.session_state:
    st.session_state.larvae_batch_action = None
if "larvae_batch_edit_id" not in st.session_state:
    st.session_state.larvae_batch_edit_id = None
if "breeding_room_list_page" not in st.session_state:
    st.session_state.breeding_room_list_page = 1
if "larvae_batch_list_page" not in st.session_state:
    st.session_state.larvae_batch_list_page = 1

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
    st.session_state.announcement_action = None
    st.session_state.target_announcement = None


menu_left, menu_center, menu_right = st.sidebar.columns([0.05, 0.9, 0.05])
MENU_OPTIONS = [
    "全場總覽與待換土提醒",
    "產房管理",
    "幼蟲管理",
    "個體清單與檔案管理",
    "血統與族譜分析",
    "新增個體與成長紀錄",
    "QR Code 掃描與識別",
    "通知管理",
    "備份/匯入",
]
DEFAULT_MENU = "全場總覽與待換土提醒"

# 優先使用 segmented_control（較美觀），若部署環境不支援或渲染行為異常，退回到 selectbox
try:
    menu = menu_center.segmented_control(
        "",
        MENU_OPTIONS,
        default=DEFAULT_MENU,
        key="main_menu",
        on_change=clear_action_on_menu_change,
    )
except Exception:
    menu = st.sidebar.selectbox(
        "",
        MENU_OPTIONS,
        index=MENU_OPTIONS.index(DEFAULT_MENU),
        key="main_menu",
        on_change=clear_action_on_menu_change,
    )

# ==========================================
# 頁面 1: 全場總覽與待換土提醒
# ==========================================
if menu == "全場總覽與待換土提醒":
    st.title("全場總覽與待換土提醒")

    # 直接讀取 beetles，避免用 table_exists("beetles") 的欄位檢查影響資料統計。
    # beetles 的資料主鍵/識別欄位可能不是 id，因此這裡以實際存在的 beetle_code 判斷有效個體。
    try:
        res_b = supabase.table("beetles").select("*").execute()
        beetle_records = res_b.data if res_b.data else []
    except Exception as ex:
        beetle_records = []
        st.error(f"讀取 beetles 資料表失敗：{ex}")

    res_l = supabase.table("logs").select("*").execute() if table_exists("logs") else None
    res_larvae = supabase.table("larvae_batches").select("*").execute() if table_exists("larvae_batches") else None

    df_beetles = pd.DataFrame(beetle_records)
    df_logs = pd.DataFrame(res_l.data if res_l and res_l.data else [])
    df_larvae = pd.DataFrame(res_larvae.data if res_larvae and res_larvae.data else [])

    # --------------------------------------------------
    # 列管數量統計
    # --------------------------------------------------
    # 先取得 beetles 中有有效 beetle_code 的個體，再從中排除死亡個體。
    if not df_beetles.empty and "beetle_code" in df_beetles.columns:
        df_valid = df_beetles[
            df_beetles["beetle_code"].notna()
            & (
                df_beetles["beetle_code"]
                .astype(str)
                .str.strip()
                .ne("")
            )
        ].copy()
    else:
        df_valid = pd.DataFrame()

    # current_stage 不存在時，一律視為「未設定」，仍然算列管資料。
    if not df_valid.empty:
        if "current_stage" not in df_valid.columns:
            df_valid["current_stage"] = "未設定"
        else:
            df_valid["current_stage"] = (
                df_valid["current_stage"]
                .fillna("未設定")
                .astype(str)
                .str.strip()
                .replace("", "未設定")
            )

    # 活體列管數量 = 有效個體中明確排除「死亡」狀態。
    # 這裡只使用 df_active 作為全場總覽的列管數量來源，
    # 避免把包含死亡紀錄的 df_valid 直接拿來計數。
    if not df_valid.empty:
        stage_normalized = (
            df_valid["current_stage"]
            .fillna("")
            .astype(str)
            .str.replace("\u3000", " ", regex=False)
            .str.strip()
        )
        df_active = df_valid[
            stage_normalized.ne("死亡")
        ].copy()
    else:
        df_active = pd.DataFrame()

    total_active_beetles = len(df_active)

    if not df_active.empty:
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

    larvae_batch_total = len(df_larvae) if not df_larvae.empty else 0
    pending_larvae_batches = 0
    # 產房資料：若 Supabase 有此表則讀取，否則保留空 DataFrame
    df_rooms = pd.DataFrame()
    rooms_total = 0
    pending_rooms = 0
    if table_exists("breeding_rooms"):
        try:
            res_rooms_local = supabase.table("breeding_rooms").select("*").execute()
            df_rooms = pd.DataFrame(res_rooms_local.data if res_rooms_local and res_rooms_local.data else [])
            rooms_total = len(df_rooms) if not df_rooms.empty else 0
        except Exception:
            df_rooms = pd.DataFrame()
            rooms_total = 0
    if not df_larvae.empty:
        for _, batch in df_larvae.iterrows():
            stage = batch.get("current_stage", "未設定")
            if stage in ["蛹", "成蟲", "死亡"]:
                continue
            maintenance_days = batch.get("maintenance_days")
            try:
                maintenance_days = int(maintenance_days) if maintenance_days not in [None, "", pd.NA] else 60
            except (TypeError, ValueError):
                maintenance_days = 60
            harvest_date = batch.get("harvest_date")
            try:
                days_passed = (date.today() - datetime.strptime(str(harvest_date), "%Y-%m-%d").date()).days
            except (TypeError, ValueError):
                days_passed = 999
            if days_passed >= maintenance_days:
                pending_larvae_batches += 1

    # 概況指標（加入產房批次數量）
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric("總列管數量 (活體)", f"{total_active_beetles} 隻")
    col2.metric("幼蟲數量", f"{larvae_cnt} 隻")
    col3.metric("化蛹數量", f"{pupa_cnt} 隻")
    col4.metric("成蟲數量", f"{adult_cnt} 隻")
    col5.metric("幼蟲批次數量", f"{larvae_batch_total} 批")
    col6.metric("產房數量", f"{rooms_total} 間")

    if pending_larvae_batches:
        st.warning(f"目前有 **{pending_larvae_batches}** 個幼蟲批次達到換土/維護條件，請至「幼蟲管理」頁面檢視。")

    # 產房到期提醒：若結束日期已到或已過，顯示提醒
    if not df_rooms.empty and "end_date" in df_rooms.columns:
        for _, room in df_rooms.iterrows():
            end_date = room.get("end_date")
            if not end_date:
                continue
            try:
                end_dt = datetime.strptime(str(end_date), "%Y-%m-%d").date()
                if end_dt <= date.today():
                    pending_rooms += 1
            except (TypeError, ValueError):
                continue

    if pending_rooms:
        st.warning(f"目前有 **{pending_rooms}** 間產房已到達/超過結束日期，請至「產房管理」頁面檢視與處理。")

    st.markdown("---")

    ann_header_col1, ann_header_col2 = st.columns([8, 2])
    ann_header_col1.subheader("系統佈告欄")
    if ann_header_col2.button("新增公告", use_container_width=True, type="primary"):
        st.session_state.announcement_action = "add"

    res_ann = supabase.table("announcements").select("*").order("created_at", desc=True).execute()
    announcements = res_ann.data if res_ann.data else []

    # 公告清單分頁：每頁 10 筆
    page_size = 10
    total_pages = max(1, (len(announcements) + page_size - 1) // page_size)
    if "announcement_page" not in st.session_state:
        st.session_state.announcement_page = 1

    ann_page = st.number_input("公告頁次", min_value=1, max_value=total_pages, value=min(st.session_state.announcement_page, total_pages), step=1, key="announcement_page")
    st.caption(f"第 {ann_page} / {total_pages} 頁，共 {len(announcements)} 筆公告")

    if not announcements:
        st.info("目前尚無任何公告。")
    else:
        start_idx = (ann_page - 1) * page_size
        end_idx = start_idx + page_size
        for ann in announcements[start_idx:end_idx]:
            with st.expander(f"{ann.get('title', '無標題')} ({ann.get('created_at', '')[:10]})"):
                st.write(ann.get('content', ""))
                
                btn_col1, btn_col2, _ = st.columns([1, 1, 8])
                if btn_col1.button("編輯", key=f"edit_ann_{ann['id']}"):
                    st.session_state.announcement_action = "edit"
                    st.session_state.target_announcement = ann
                    st.rerun()
                if btn_col2.button("刪除", key=f"del_ann_{ann['id']}"):
                    st.session_state.announcement_action = "delete"
                    st.session_state.target_announcement = ann
                    st.rerun()

    @st.dialog("佈告欄管理")
    def render_announcement_dialog():
        action = st.session_state.get("announcement_action")
        target = st.session_state.get("target_announcement")

        if action == "add":
            st.subheader("新增公告")
            with st.form("add_announcement_form"):
                new_title = st.text_input("公告標題 (必填)")
                new_content = st.text_area("公告內容 (必填)")
                if st.form_submit_button("發布公告", type="primary"):
                    if not new_title.strip() or not new_content.strip():
                        st.error("標題與內容皆為必填！")
                    else:
                        supabase.table("announcements").insert({
                            "title": new_title.strip(),
                            "content": new_content.strip(),
                            "created_at": datetime.now().isoformat(timespec="seconds")
                        }).execute()
                        st.session_state.announcement_action = None
                        st.success("公告已發布！")
                        st.rerun()

        elif action == "edit" and target:
            st.subheader("編輯公告")
            with st.form("edit_announcement_form"):
                edit_title = st.text_input("公告標題", value=target.get("title", ""))
                edit_content = st.text_area("公告內容", value=target.get("content", ""))
                if st.form_submit_button("儲存修改", type="primary"):
                    if not edit_title.strip() or not edit_content.strip():
                        st.error("標題與內容皆為必填！")
                    else:
                        supabase.table("announcements").update({
                            "title": edit_title.strip(),
                            "content": edit_content.strip(),
                        }).eq("id", target["id"]).execute()
                        st.session_state.announcement_action = None
                        st.session_state.target_announcement = None
                        st.success("公告修改成功！")
                        st.rerun()

        elif action == "delete" and target:
            st.error(f"確定要刪除公告「{target.get('title')}」嗎？")
            if st.button("確認刪除", type="primary"):
                supabase.table("announcements").delete().eq("id", target["id"]).execute()
                st.session_state.announcement_action = None
                st.session_state.target_announcement = None
                st.success("公告已刪除！")
                st.rerun()

        if st.button("關閉"):
            st.session_state.announcement_action = None
            st.session_state.target_announcement = None
            st.rerun()

    if st.session_state.get("announcement_action"):
        render_announcement_dialog()

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
        st.info("目前列管資料皆為死亡個體，沒有需要換土/維護的活體。")
    else:
        st.success("全場狀況良好，目前沒有到達換土週期的個體！")

# ==========================================
# 頁面 2: 產房管理
# ==========================================
elif menu == "產房管理":
    st.title("產房管理")
    st.caption("記錄種親交配、產房投產與結束日期，掌握每個產房的完整生命週期。")

    if table_exists("breeding_rooms"):
        res_rooms = supabase.table("breeding_rooms").select("*").order("start_date", desc=True).execute()
        rooms = res_rooms.data if res_rooms.data else []
    else:
        rooms = []
    df_rooms = pd.DataFrame(rooms)

    if not table_exists("breeding_rooms"):
        st.warning("目前 Supabase 尚未建立 breeding_rooms 資料表，請先在資料庫中執行建立 SQL 後再使用此功能。")
    else:
        with st.form("breeding_room_add_form", clear_on_submit=True):
            st.subheader("新增產房")
            room_cols = st.columns(3)
            room_code = room_cols[0].text_input("產房編號 (必填)", placeholder="例: ROOM-2026-01")
            beetle_info = room_cols[1].text_input("種親 / 甲蟲對應", placeholder="例: DHH-M01 x DHH-F02")
            status = room_cols[2].selectbox("狀態", ["開始", "結束"], index=0)

            date_cols = st.columns(3)
            start_date = date_cols[0].date_input("開始日期", value=date.today())
            end_date = date_cols[1].date_input("結束日期（選填）", value=None)
            notes = date_cols[2].text_area("備註", placeholder="請輸入產房備註...")

            if st.form_submit_button("新增產房", type="primary"):
                if not room_code.strip():
                    st.error("產房編號為必填欄位。")
                else:
                    payload = {
                        "room_code": room_code.strip(),
                        "beetle_info": beetle_info.strip(),
                        "start_date": start_date.strftime("%Y-%m-%d"),
                        "end_date": end_date.strftime("%Y-%m-%d") if end_date is not None else None,
                        "status": status,
                        "notes": notes.strip(),
                    }
                    try:
                        supabase.table("breeding_rooms").insert(payload).execute()
                        st.success(f"產房 {room_code.strip()} 已新增。")
                        st.rerun()
                    except Exception as ex:
                        st.error(f"新增產房失敗：{ex}")

    st.markdown("---")
    st.subheader("產房列表")

    keyword = st.text_input("關鍵字搜尋", placeholder="輸入產房編號、種親說明或備註")
    status_filter = st.selectbox("狀態篩選", ["全部", "開始", "結束"], index=0)

    if not df_rooms.empty:
        filtered_rooms = df_rooms.copy()
        if status_filter != "全部":
            filtered_rooms = filtered_rooms[filtered_rooms["status"].fillna("").astype(str) == status_filter]
        if keyword.strip():
            keyword_lower = keyword.strip().lower()
            filtered_rooms = filtered_rooms[
                filtered_rooms["room_code"].fillna("").astype(str).str.lower().str.contains(keyword_lower, na=False)
                | filtered_rooms["beetle_info"].fillna("").astype(str).str.lower().str.contains(keyword_lower, na=False)
                | filtered_rooms["notes"].fillna("").astype(str).str.lower().str.contains(keyword_lower, na=False)
            ]

        if filtered_rooms.empty:
            st.info("查無符合條件的產房資料。")
        else:
            page_size = 10
            total_pages = max(1, (len(filtered_rooms) + page_size - 1) // page_size)

            def clear_breeding_room_action_on_page_change():
                st.session_state.breeding_room_action = None
                st.session_state.breeding_room_edit_id = None

            current_page = min(
                int(st.session_state.get("breeding_room_list_page", 1)),
                total_pages,
            )
            page_number = st.number_input(
                "頁次",
                min_value=1,
                max_value=total_pages,
                value=current_page,
                step=1,
                key="breeding_room_list_page",
                on_change=clear_breeding_room_action_on_page_change,
            )
            page_start = (page_number - 1) * page_size
            page_df = filtered_rooms.iloc[page_start:page_start + page_size]
            st.caption(f"第 {page_number} / {total_pages} 頁，共 {len(filtered_rooms)} 筆")

            for _, room in page_df.iterrows():
                with st.expander(f"{room.get('room_code', '未命名產房')}｜{room.get('status', '開始')}｜{room.get('start_date', '')}"):
                    c1, c2, c3 = st.columns(3)
                    c1.write(f"**產房編號：** {room.get('room_code', '-')}")
                    c2.write(f"**種親/甲蟲對應：** {room.get('beetle_info', '-')}")
                    c3.write(f"**狀態：** {room.get('status', '開始')}")
                    c4, c5, c6 = st.columns(3)
                    c4.write(f"**開始日期：** {room.get('start_date', '-')}")
                    c5.write(f"**結束日期：** {room.get('end_date') or '尚未結束'}")
                    c6.write(f"**備註：** {room.get('notes') or '無'}")

                    edit_col, delete_col = st.columns(2)
                    if edit_col.button("編輯", key=f"edit_room_{room.get('room_code')}_{room.get('id')}"):
                        st.session_state.breeding_room_action = "edit"
                        st.session_state.breeding_room_edit_id = room.get("id")
                    if delete_col.button("刪除", key=f"delete_room_{room.get('room_code')}_{room.get('id')}"):
                        supabase.table("breeding_rooms").delete().eq("id", room.get("id")).execute()
                        st.success("產房資料已刪除。")
                        st.rerun()

                    if st.session_state.get("breeding_room_action") == "edit" and st.session_state.get("breeding_room_edit_id") == room.get("id"):
                        with st.form(f"edit_room_form_{room.get('id')}"):
                            edit_room_code = st.text_input("產房編號", value=room.get("room_code", ""))
                            edit_beetle_info = st.text_input("種親 / 甲蟲對應", value=room.get("beetle_info", ""))
                            edit_status = st.selectbox("狀態", ["開始", "結束"], index=["開始", "結束"].index(room.get("status", "開始")))
                            edit_start = st.date_input("開始日期", value=datetime.strptime(str(room.get("start_date")), "%Y-%m-%d").date() if room.get("start_date") else date.today())
                            edit_end_raw = room.get("end_date")
                            edit_end = st.date_input("結束日期（選填）", value=datetime.strptime(str(edit_end_raw), "%Y-%m-%d").date() if edit_end_raw else None)
                            edit_notes = st.text_area("備註", value=room.get("notes") or "")
                            if st.form_submit_button("儲存修改", type="primary"):
                                payload = {
                                    "room_code": edit_room_code.strip(),
                                    "beetle_info": edit_beetle_info.strip(),
                                    "start_date": edit_start.strftime("%Y-%m-%d"),
                                    "end_date": edit_end.strftime("%Y-%m-%d") if edit_end is not None else None,
                                    "status": edit_status,
                                    "notes": edit_notes.strip(),
                                }
                                supabase.table("breeding_rooms").update(payload).eq("id", room.get("id")).execute()
                                st.session_state.breeding_room_action = None
                                st.session_state.breeding_room_edit_id = None
                                st.success("產房資料已更新。")
                                st.rerun()

    else:
        st.info("目前尚無任何產房紀錄。")

# ==========================================
# 頁面 3: 幼蟲管理
# ==========================================
elif menu == "幼蟲管理":
    st.title("幼蟲管理")
    st.caption("集中管理批次幼蟲的種群資訊、數量與換土提醒週期。")

    if table_exists("larvae_batches"):
        res_larvae = supabase.table("larvae_batches").select("*").order("harvest_date", desc=True).execute()
        larvae_batches = res_larvae.data if res_larvae.data else []
    else:
        larvae_batches = []
    df_larvae = pd.DataFrame(larvae_batches)

    if not table_exists("larvae_batches"):
        st.warning("目前 Supabase 尚未建立 larvae_batches 資料表，請先在資料庫中執行建立 SQL 後再使用此功能。")
    else:
        with st.form("larvae_batch_add_form", clear_on_submit=True):
            st.subheader("新增幼蟲批次")
            c1, c2, c3 = st.columns(3)
            batch_code = c1.text_input("批次編號 (必填)", placeholder="例: LARV-2026-01")
            species = c2.text_input("物種名稱 (必填)", placeholder="例: 赫克力士長角大カブト")
            lineage = c3.text_input("血統", placeholder="例: 極太血統")

            c4, c5, c6 = st.columns(3)
            parents = c4.text_input("親代")
            generation = c5.text_input("累代", placeholder="例: CBF1")
            father_id = c6.text_input("父 ID")

            c7, c8, c9, c10 = st.columns(4)
            mother_id = c7.text_input("母 ID")
            initial_stage = c8.selectbox("初始階段", ["一齡幼蟲", "二齡幼蟲", "三齡幼蟲", "前蛹", "蛹"], index=0)
            current_stage = c9.selectbox("當前階段", ["一齡幼蟲", "二齡幼蟲", "三齡幼蟲", "前蛹", "蛹", "成蟲", "死亡"], index=0)
            status = c10.selectbox("狀態", ["開始", "結束"], index=0)

            c10, c11, c12 = st.columns(3)
            harvest_date = c10.date_input("孵化 / 採收日期", value=date.today())
            quantity = c11.number_input("數量", min_value=1, value=1, step=1)
            maintenance_days = c12.number_input("換土週期 (天)", min_value=1, value=60, step=5)

            notes = st.text_area("備註", placeholder="請輸入批次備註...")

            if st.form_submit_button("新增幼蟲批次", type="primary"):
                if not batch_code.strip() or not species.strip():
                    st.error("批次編號與物種名稱為必填欄位。")
                else:
                    payload = {
                        "batch_code": batch_code.strip(),
                        "species": species.strip(),
                        "lineage": lineage.strip(),
                        "parents": parents.strip(),
                        "generation": generation.strip(),
                        "father_id": father_id.strip(),
                        "mother_id": mother_id.strip(),
                        "initial_stage": initial_stage,
                        "current_stage": current_stage,
                        "status": status,
                        "harvest_date": harvest_date.strftime("%Y-%m-%d"),
                        "quantity": int(quantity),
                        "maintenance_days": int(maintenance_days),
                        "notes": notes.strip(),
                    }
                    try:
                        supabase.table("larvae_batches").insert(payload).execute()
                        st.success(f"幼蟲批次 {batch_code.strip()} 已新增。")
                        st.rerun()
                    except Exception as ex:
                        st.error(f"新增幼蟲批次失敗：{ex}")

    st.markdown("---")
    st.subheader("幼蟲批次列表")

    larvae_keyword_col1, larvae_keyword_col2, larvae_keyword_col3 = st.columns([3, 3, 2])
    larvae_keyword = larvae_keyword_col1.text_input(
        "關鍵字搜尋",
        placeholder="輸入批次編號或物種",
        key="larvae_keyword",
    )
    larvae_status_filter = larvae_keyword_col2.selectbox(
        "狀態篩選",
        ["全部", "開始", "結束"],
        index=0,
        key="larvae_status_filter",
    )

    filtered_larvae = df_larvae.copy()
    if "status" not in filtered_larvae.columns:
        filtered_larvae["status"] = "開始"
    if larvae_keyword.strip():
        keyword_lower = larvae_keyword.strip().lower()
        filtered_larvae = filtered_larvae[
            filtered_larvae["batch_code"].fillna("").astype(str).str.lower().str.contains(keyword_lower, na=False)
            | filtered_larvae["species"].fillna("").astype(str).str.lower().str.contains(keyword_lower, na=False)
        ]
    if larvae_status_filter != "全部":
        filtered_larvae = filtered_larvae[
            filtered_larvae["status"].fillna("開始").astype(str) == larvae_status_filter
        ]

    if filtered_larvae.empty:
        st.info("查無符合條件的幼蟲批次資料。")
    else:
        page_size = 10
        total_pages = max(1, (len(filtered_larvae) + page_size - 1) // page_size)

        def clear_larvae_action_on_page_change():
            st.session_state.larvae_batch_action = None
            st.session_state.larvae_batch_edit_id = None

        current_page = min(
            int(st.session_state.get("larvae_batch_list_page", 1)),
            total_pages,
        )
        page_number = st.number_input(
            "頁次",
            min_value=1,
            max_value=total_pages,
            value=current_page,
            step=1,
            key="larvae_batch_list_page",
            on_change=clear_larvae_action_on_page_change,
        )
        page_start = (page_number - 1) * page_size
        page_df = filtered_larvae.iloc[page_start:page_start + page_size]
        st.caption(f"第 {page_number} / {total_pages} 頁，共 {len(filtered_larvae)} 筆")

        for _, batch in page_df.iterrows():
            with st.expander(f"{batch.get('batch_code', '未命名批次')}｜{batch.get('species', '-') }｜{batch.get('current_stage', '一齡幼蟲')}"):
                cols = st.columns(4)
                cols[0].write(f"**批次編號：** {batch.get('batch_code', '-')}")
                cols[1].write(f"**物種：** {batch.get('species', '-')}")
                cols[2].write(f"**血統：** {batch.get('lineage') or '無'}")
                cols[3].write(f"**累代：** {batch.get('generation') or '無'}")

                c5, c6, c7, c8 = st.columns(4)
                c5.write(f"**親代：** {batch.get('parents') or '無'}")
                c6.write(f"**父/母 ID：** {batch.get('father_id') or '-'} / {batch.get('mother_id') or '-'}")
                c7.write(f"**初始階段：** {batch.get('initial_stage', '-')}")
                c8.write(f"**當前階段：** {batch.get('current_stage', '-')}")

                c9, c10, c11, c12 = st.columns(4)
                c9.write(f"**孵化/採收日期：** {batch.get('harvest_date') or '-'}")
                c10.write(f"**數量：** {batch.get('quantity') or 0}")
                c11.write(f"**換土週期：** {batch.get('maintenance_days') or 60} 天")
                c12.write(f"**狀態：** {batch.get('status') or '開始'}")
                st.write(f"**備註：** {batch.get('notes') or '無'}")

                edit_col, delete_col = st.columns(2)
                if edit_col.button("編輯", key=f"edit_larvae_{batch.get('batch_code')}_{batch.get('id')}"):
                    st.session_state.larvae_batch_action = "edit"
                    st.session_state.larvae_batch_edit_id = batch.get("id")
                if delete_col.button("刪除", key=f"delete_larvae_{batch.get('batch_code')}_{batch.get('id')}"):
                    supabase.table("larvae_batches").delete().eq("id", batch.get("id")).execute()
                    st.success("幼蟲批次已刪除。")
                    st.rerun()

                if st.session_state.get("larvae_batch_action") == "edit" and st.session_state.get("larvae_batch_edit_id") == batch.get("id"):
                    with st.form(f"edit_larvae_form_{batch.get('id')}"):
                        edit_c1, edit_c2, edit_c3 = st.columns(3)
                        edit_batch_code = edit_c1.text_input("批次編號", value=batch.get("batch_code", ""))
                        edit_species = edit_c2.text_input("物種名稱", value=batch.get("species", ""))
                        edit_lineage = edit_c3.text_input("血統", value=batch.get("lineage") or "")

                        edit_c4, edit_c5, edit_c6 = st.columns(3)
                        edit_parents = edit_c4.text_input("親代", value=batch.get("parents") or "")
                        edit_generation = edit_c5.text_input("累代", value=batch.get("generation") or "")
                        edit_father_id = edit_c6.text_input("父 ID", value=batch.get("father_id") or "")

                        edit_c7, edit_c8, edit_c9, edit_c10 = st.columns(4)
                        edit_mother_id = edit_c7.text_input("母 ID", value=batch.get("mother_id") or "")
                        edit_initial_stage = edit_c8.selectbox("初始階段", ["一齡幼蟲", "二齡幼蟲", "三齡幼蟲", "前蛹", "蛹"], index=["一齡幼蟲", "二齡幼蟲", "三齡幼蟲", "前蛹", "蛹"].index(batch.get("initial_stage", "一齡幼蟲")))
                        edit_current_stage = edit_c9.selectbox("當前階段", ["一齡幼蟲", "二齡幼蟲", "三齡幼蟲", "前蛹", "蛹", "成蟲", "死亡"], index=["一齡幼蟲", "二齡幼蟲", "三齡幼蟲", "前蛹", "蛹", "成蟲", "死亡"].index(batch.get("current_stage", "一齡幼蟲")))
                        edit_status_val = batch.get("status", "開始") or "開始"
                        edit_status = edit_c10.selectbox("狀態", ["開始", "結束"], index=["開始", "結束"].index(edit_status_val) if edit_status_val in ["開始", "結束"] else 0)

                        edit_c10, edit_c11, edit_c12 = st.columns(3)
                        initial_harvest = batch.get("harvest_date")
                        harvest_value = datetime.strptime(str(initial_harvest), "%Y-%m-%d").date() if initial_harvest else date.today()
                        edit_harvest_date = edit_c10.date_input("孵化 / 採收日期", value=harvest_value)
                        edit_quantity = edit_c11.number_input("數量", min_value=1, value=int(batch.get("quantity") or 1), step=1)
                        edit_maintenance_days = edit_c12.number_input("換土週期 (天)", min_value=1, value=int(batch.get("maintenance_days") or 60), step=5)
                        edit_notes = st.text_area("備註", value=batch.get("notes") or "")

                        if st.form_submit_button("儲存修改", type="primary"):
                            payload = {
                                "batch_code": edit_batch_code.strip(),
                                "species": edit_species.strip(),
                                "lineage": edit_lineage.strip(),
                                "parents": edit_parents.strip(),
                                "generation": edit_generation.strip(),
                                "father_id": edit_father_id.strip(),
                                "mother_id": edit_mother_id.strip(),
                                "initial_stage": edit_initial_stage,
                                "current_stage": edit_current_stage,
                                "status": edit_status,
                                "harvest_date": edit_harvest_date.strftime("%Y-%m-%d"),
                                "quantity": int(edit_quantity),
                                "maintenance_days": int(edit_maintenance_days),
                                "notes": edit_notes.strip(),
                            }
                            supabase.table("larvae_batches").update(payload).eq("id", batch.get("id")).execute()
                            st.session_state.larvae_batch_action = None
                            st.session_state.larvae_batch_edit_id = None
                            st.success("幼蟲批次已更新。")
                            st.rerun()

# ==========================================
# 頁面 4: 個體清單與檔案管理
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
                action_cols = st.columns(7)
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
                if action_cols[3].button("血統", key=f"pedigree_{action_key}", use_container_width=True):
                    st.session_state.current_action = "pedigree"
                    st.session_state.edit_target_code = row_code
                if action_cols[4].button("QR", key=f"qr_{action_key}"):
                    st.session_state.current_action = "qr"
                    st.session_state.edit_target_code = row_code
                if action_cols[5].button("刪除", key=f"delete_{action_key}", use_container_width=True):
                    st.session_state.current_action = "delete"
                    st.session_state.edit_target_code = row_code
                if action_cols[6].button("圖片", key=f"images_{action_key}", use_container_width=True):
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
                    
                    f_id_val = selected_info.get('father_id', '')
                    m_id_val = selected_info.get('mother_id', '')
                    formatted_parents = format_parent_display(f_id_val, m_id_val)
                    v_col3.write(f"**父/母 ID:** {formatted_parents}")

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

                elif st.session_state.get("current_action") == "pedigree":
                    st.subheader(f"{active_code} 血統樹/族譜圖")
                    dot_str = generate_pedigree_dot(active_code, depth=3, show_offspring=False)
                    st.graphviz_chart(dot_str)

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
# 頁面 3: 血統與族譜分析 (獨立分頁)
# ==========================================
elif menu == "血統與族譜分析":
    st.title("血統與族譜分析")
    st.caption("自動繪製個體的直系血統樹，釐清血統來源並輔助履歷展示。")

    res_b = supabase.table("beetles").select("beetle_code, custom_id, species").execute()
    beetle_options = res_b.data if res_b.data else []

    if not beetle_options:
        st.info("目前資料庫無個體資料，請先新增個體。")
    else:
        # 新增 ID 搜尋框與物種搜尋框
        filter_c1, filter_c2 = st.columns(2)
        filter_id_input = filter_c1.text_input("過濾 ID 搜尋", placeholder="輸入 ID 關鍵字...", key="pedigree_filter_id")
        filter_species_input = filter_c2.text_input("過濾物種搜尋", placeholder="輸入物種關鍵字...", key="pedigree_filter_species")

        # 根據搜尋條件過濾選項
        filtered_beetles = []
        for b in beetle_options:
            code = b.get("beetle_code", "")
            cid = str(b.get("custom_id", ""))
            sp = str(b.get("species", ""))

            match_id = not filter_id_input.strip() or (filter_id_input.strip().lower() in cid.lower() or filter_id_input.strip().lower() in code.lower())
            match_species = not filter_species_input.strip() or (filter_species_input.strip().lower() in sp.lower())

            if match_id and match_species:
                filtered_beetles.append(b)

        if not filtered_beetles:
            st.warning("找不到符合篩選條件的個體。")
        else:
            options_map = {}
            for b in filtered_beetles:
                code = b.get("beetle_code")
                cid = b.get("custom_id") or code
                sp = b.get("species", "")
                display_str = f"{cid} ({code}) - {sp}"
                options_map[display_str] = code

            ctrl_col1, ctrl_col2, ctrl_col3 = st.columns([4, 2, 2])
            selected_display = ctrl_col1.selectbox("選擇目標個體", list(options_map.keys()))
            selected_code = options_map[selected_display]

            depth = ctrl_col2.selectbox("向上追溯代數", [2, 3, 4], index=1, help="2代: 祖父母 | 3代: 曾祖父母 | 4代: 高祖父母")
            show_offspring = ctrl_col3.checkbox("顯示向下第一代子代", value=False)

            st.markdown("---")

            dot_code = generate_pedigree_dot(selected_code, depth=depth, show_offspring=show_offspring)

            chart_col, code_col = st.tabs(["血統圖表渲染", "📜 DOT 原始碼與匯出"])

            with chart_col:
                st.graphviz_chart(dot_code)

            with code_col:
                st.code(dot_code, language="dot")
                st.download_button(
                    label="📥 下載 DOT 檔案",
                    data=dot_code,
                    file_name=f"pedigree_{selected_code}.dot",
                    mime="text/vnd.graphviz"
                )

# ==========================================
# 頁面 4: 新增個體與成長紀錄
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

    with st.form("create_beetle_full_form", clear_on_submit=True, enter_to_submit=False):
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
# 頁面 5: QR Code 掃描與識別
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
        image = Image.open(img_input)
        st.image(image, caption="待掃描影像", width=300)
        
        img_np = np.array(image.convert("RGB"))
        img_cv = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)

        detector = cv2.QRCodeDetector()
        qr_content, bbox, _ = detector.detectAndDecode(img_cv)

        if qr_content:
            st.success("🎉 自動辨識解碼成功！")
            return qr_content
        else:
            st.error("❌ 照片中未偵測到有效的 QR Code，請確認照片對焦清晰並重試。")
            return None

    with scan_tab1:
        st.markdown("#### 拍照辨識")
        camera_img = st.camera_input("請將鏡頭對準標籤 QR Code 並按下拍照", key="qr_camera_input")
        if camera_img is not None:
            decoded_json_str = process_qr_image(camera_img)

    with scan_tab2:
        st.markdown("#### 檔案上傳")
        img_file = st.file_uploader("請上傳 QR Code 標籤圖片", type=["png", "jpg", "jpeg", "webp"], key="qr_file_uploader")
        if img_file is not None:
            decoded_json_str = process_qr_image(img_file)

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
# 頁面 6: 通知管理
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
# 頁面 7: 備份/匯入
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