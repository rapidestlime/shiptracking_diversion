import gspread
from google.oauth2.service_account import Credentials
import json
import os
from dotenv import load_dotenv
from datetime import datetime
from zoneinfo import ZoneInfo
import streamlit as st



SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Expected columns (must match your sheet header row exactly)
COLUMNS = [
    "Last_Updated",
    "IMO",
    "Name",
    "KPLER_ID",
    "Departure",
    "Coord_Trace",
    "Original_Dest",
    "Original_Dest_Lat",
    "Original_Dest_Long",
    "Cargo"
]


class GSheet_Handler():
    def __init__(self, use_streamlit=True):
        """
        :param use_streamlit: Set to False if running a local script without Streamlit
        """
        if use_streamlit:
            self._load_from_streamlit()
        else:
            self._load_from_local()
            
        self.client = self.get_client()
        self.sheet = self.get_sheet()

    def _load_from_streamlit(self):
        # 1. Handle Credentials (Check if it's a string or dict)
        creds = st.secrets["GSHEET_CREDENTIALS"]
        creds = creds.replace('\n', '').replace('\r', '')
        self.GSHEET_CREDENTIALS = json.loads(creds) if isinstance(creds, str) else creds
        
        # 2. Handle IDs
        self.SPREADSHEET_ID = st.secrets["SPREADSHEET_ID"]
        self.SHEET_NAME = st.secrets["SHEET_NAME"]

    def _load_from_local(self):
        load_dotenv('secrets.env')
        # Standard local env loading
        creds_raw = os.getenv("GSHEET_CREDENTIALS")
        self.GSHEET_CREDENTIALS = json.loads(creds_raw) if creds_raw else {}
        self.SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
        self.SHEET_NAME = os.getenv("SHEET_NAME")

    def get_client(self):
        """
        Initialise GSheet Client
        """
        creds = Credentials.from_service_account_info(self.GSHEET_CREDENTIALS, scopes=SCOPES)
        return gspread.authorize(creds)
    
    def get_sheet(self):
        """
        Initialise Sheet Object
        """
        sheet = self.client.open_by_key(self.SPREADSHEET_ID).worksheet(self.SHEET_NAME)
        return sheet
    
    
# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_row(row: list) -> dict:
    """Map a raw sheet row (list) → structured dict."""
    padded = row + [""] * (len(COLUMNS) - len(row))   # guard short rows
    record = dict(zip(COLUMNS, padded))

    # Deserialise the positions JSON column
    raw_positions = record.get("Coord_Trace", "")
    try:
        record["Coord_Trace"] = json.loads(raw_positions) if raw_positions else []
    except json.JSONDecodeError:
        record["Coord_Trace"] = []

    return record

def serialise_record(record: dict) -> list:
    """Map a structured dict → sheet row (list), ready to write back."""
    row = []
    for col in COLUMNS:
        value = record.get(col, "")
        if col == "Coord_Trace":
            value = json.dumps(value, separators=(",", ":"))   # compact JSON
        row.append(value)
    return row

# ── Read ──────────────────────────────────────────────────────────────────────

def get_all_ships(sheet) -> list[dict]:
    """Return every ship record as a list of dicts.
    
    :param: sheet: Initialised GSheet Object
    """
    rows   = sheet.get_all_values()

    if not rows:
        return []

    header, *data_rows = rows
    return [parse_row(row) for row in data_rows if any(row)]


def get_ship_by_id(ship_id: int | str) -> dict | None:
    """Find a single ship record by ship_id."""
    for record in get_all_ships():
        if str(record.get("KPLER_ID")) == str(ship_id):
            return record
    return None
    

# ── Write ─────────────────────────────────────────────────────────────────────

def upsert_ship(sheet, row_index: int, updated_record: dict):
    """Overwrite a specific row in the sheet (row_index is 1-based)."""
    
    # 1. Set the timestamp before the loop
    now_ts = datetime.now(ZoneInfo("Asia/Singapore")).isoformat(timespec="seconds")
    updated_record["Last_Updated"] = now_ts

    WRITE_COLUMNS = [
        "Last_Updated",
        "IMO",
        "Name",
        "KPLER_ID",
        "Departure",
        "Coord_Trace",
    ]

    row_data = []
    for col in WRITE_COLUMNS:
        value = updated_record.get(col, "")
        
        # 2. Handle specific formatting
        if col == "Coord_Trace":
            value = json.dumps(value) if value else "[]"
        
        row_data.append(value)

    # 3. Correct Range Logic
    # We want "A{row}:F{row}"
    num_cols = len(WRITE_COLUMNS)
    end_col_letter = gspread.utils.rowcol_to_a1(row=row_index,col=num_cols) # Returns "F"
    range_label = f"A{row_index}:{end_col_letter}{row_index}"

    sheet.update(range_name=range_label, values=[row_data])