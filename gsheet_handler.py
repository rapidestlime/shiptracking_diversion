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
    "Diversion_Flag",
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

def get_all_ships(sheet) -> tuple[list[dict], dict]:
    """Return every ship record as a list of dicts.
    
    :param: sheet: Initialised GSheet Object
    """
    rows   = sheet.get_all_values()

    if not rows:
        return []
    # Create the header map using gspread utility
    header, *data_rows = rows
    header_map = {
        name: gspread.utils.rowcol_to_a1(1, i + 1).replace("1", "") 
        for i, name in enumerate(header)
    }
    ships = [parse_row(row) for row in data_rows if any(row)]

    return ships, header_map


def get_ship_by_id(ship_id: int | str) -> dict | None:
    """Find a single ship record by ship_id."""
    for record in get_all_ships():
        if str(record.get("KPLER_ID")) == str(ship_id):
            return record
    return None
    

# ── Write ─────────────────────────────────────────────────────────────────────

def upsert_ship(sheet, row_index: int, updated_record: dict, header_map: dict):
    """Overwrite a specific row in the sheet (row_index is 1-based)."""
    
    # 1. Set the timestamp before the loop
    now_ts = datetime.now(ZoneInfo("Asia/Singapore")).isoformat(timespec="seconds")
    updated_record["Last_Updated"] = now_ts

    ### old code 1 ###
    # WRITE_COLUMNS = [
    #     "Last_Updated",
    #     "IMO",
    #     "Name",
    #     "KPLER_ID",
    #     "Departure",
    #     "Coord_Trace",
    #     "Diversion_Flagged"
    # ]

    # row_data = []
    # for col in WRITE_COLUMNS:
    #     value = updated_record.get(col, "")
        
    #     # 2. Handle specific formatting
    #     if col == "Coord_Trace":
    #         value = json.dumps(value) if value else "[]"
        
    #     row_data.append(value)

    # # 3. Correct Range Logic
    # # We want "A{row}:F{row}"
    # num_cols = len(WRITE_COLUMNS)
    # end_col_letter = gspread.utils.rowcol_to_a1(row=row_index,col=num_cols) # Returns "F"
    # range_label = f"A{row_index}:{end_col_letter}{row_index}"

    # sheet.update(range_name=range_label, values=[row_data])
    
    ### old code 2 ###
    # # 1. Get the current row values
    # existing_row = sheet.get_values(
    #     f"A{row_index}:{row_index}", 
    #     value_render_option='FORMULA'
    # )[0]

    # # 2. Map your header names to their actual column indices (1-based)
    # # Assuming you have a header row at index 1
    # headers = sheet.row_values(1) 
    # header_to_idx = {name: i for i, name in enumerate(headers)}

    # # 3. Update only the fields present in updated_record
    # for key, value in updated_record.items():
    #     if key in header_to_idx:
    #         idx = header_to_idx[key]
    #         # Ensure the list is long enough
    #         while len(existing_row) <= idx:
    #             existing_row.append("")
            
    #         if key == "Coord_Trace":
    #             value = json.dumps(value) if value else "[]"

    #         elif key == "Diversion_Flag":
    #             value = json.dumps(value) if value else "[]"
                
    #         existing_row[idx] = value

    # # 4. Push the full row back
    # sheet.update(f"A{row_index}", [existing_row])

    # 2. Prepare the list of specific cell updates
    batch_data = []

    for key, value in updated_record.items():
        if key in header_map:
            col_letter = header_map[key]
            
            # 3. Handle specific formatting
            if key == "Coord_Trace":
                value = json.dumps(value) if value else "[]"
            
            # Note: For Google Sheets checkboxes, usually a raw bool is better than json.dumps
            elif key == "Diversion_Flag":
                value = bool(value)

            # 4. Create the update entry for this specific column
            batch_data.append({
                'range': f"{col_letter}{row_index}",
                'values': [[value]]
            })

    # 5. Push all changes in ONE API call
    if batch_data:
        # USER_ENTERED ensures numbers/dates/booleans are parsed correctly by Google
        sheet.batch_update(batch_data, value_input_option='USER_ENTERED')