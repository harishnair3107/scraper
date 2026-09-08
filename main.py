import sys
import os
import io
import re
import pandas as pd
import mysql.connector
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import traceback
from datetime import datetime

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", "chickenfry31"),
        database=os.getenv("DB_NAME", "rating_tool"),
        port=int(os.getenv("DB_PORT", 3306))
    )


def sanitize_column_name(col):
    # Remove special chars, replace spaces with underscores
    col = str(col).lower().strip()
    col = re.sub(r'[^a-z0-9_]', '_', col)
    col = re.sub(r'_+', '_', col)
    return col

@app.post("/upload_excel")
async def upload_excel_endpoint(file: UploadFile = File(...), custom_name: str = Form(None)):
    try:
        contents = await file.read()
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        
        # Clean filename for table name
        if custom_name and custom_name.strip():
            clean_name = re.sub(r'[^a-z0-9_]', '', custom_name.strip().lower())
            table_name = f"upload_{clean_name}"
        else:
            clean_name = re.sub(r'[^a-z0-9]', '', file.filename.lower().split('.')[0])
            table_name = f"upload_{timestamp}_{clean_name}"
        
        save_filename = f"{timestamp}_{file.filename}"
        save_path = "stored_in_db"
        
        filename_lower = file.filename.lower()
        if filename_lower.endswith('.csv'):
            df = pd.read_csv(io.BytesIO(contents))
        elif filename_lower.endswith('.xlsx') or filename_lower.endswith('.xls'):
            df = pd.read_excel(io.BytesIO(contents))
        else:
            raise Exception("Unsupported file type.")
            
        if df.empty:
            raise Exception("File is empty.")
            
        total_rows = len(df)
        
        # Ensure a keyword column exists
        raw_cols = df.columns
        cleaned_cols = [sanitize_column_name(c) for c in raw_cols]
        df.columns = cleaned_cols
        
        has_keyword = any(c in cleaned_cols for c in ['keyword', 'brand', 'brand_name', 'link', 'url', 'name'])
        if not has_keyword:
            raise Exception("Could not identify a keyword column. Please ensure a column named 'keyword', 'brand', or 'link' exists.")
            
        # Build CREATE TABLE query
        db = get_db_connection()
        cursor = db.cursor()
        
        create_sql = f"""
        CREATE TABLE {table_name} (
            id INT AUTO_INCREMENT PRIMARY KEY,
        """
        for col in cleaned_cols:
            if col == 'id': continue # conflict prevention
            create_sql += f"`{col}` TEXT,\n"
            
        create_sql += """
            scrape_status VARCHAR(20) DEFAULT 'Pending',
            google_rating VARCHAR(10) DEFAULT NULL,
            google_reviews VARCHAR(20) DEFAULT NULL,
            zomato_rating VARCHAR(10) DEFAULT NULL,
            zomato_reviews VARCHAR(20) DEFAULT NULL,
            magicpin_rating VARCHAR(10) DEFAULT NULL,
            magicpin_reviews VARCHAR(20) DEFAULT NULL,
            justdial_rating VARCHAR(10) DEFAULT NULL,
            justdial_reviews VARCHAR(20) DEFAULT NULL
        )
        """
        cursor.execute(create_sql)
        
        # Insert rows in chunks to prevent MySQL Lost Connection (Error 2013/2006) for large files
        cols_to_insert = [c for c in cleaned_cols if c != 'id']
        placeholders = ", ".join(["%s"] * len(cols_to_insert))
        cols_formatted = ", ".join([f"`{c}`" for c in cols_to_insert])
        
        insert_sql = f"INSERT INTO {table_name} ({cols_formatted}) VALUES ({placeholders})"
        
        batch_data = []
        chunk_size = 2000
        
        for _, row in df.iterrows():
            row_data = tuple(str(row[c]) if pd.notna(row[c]) else "" for c in cols_to_insert)
            batch_data.append(row_data)
            
            if len(batch_data) >= chunk_size:
                cursor.executemany(insert_sql, batch_data)
                batch_data = []
                
        if batch_data:
            cursor.executemany(insert_sql, batch_data)
        
        display_name = custom_name.strip() if custom_name and custom_name.strip() else file.filename
        
        # Log to uploads master table
        cursor.execute("""
            INSERT INTO uploads (filename, file_path, total_rows, processed_rows, table_name)
            VALUES (%s, %s, %s, 0, %s)
        """, (display_name, save_path, total_rows, table_name))
        
        db.commit()
        cursor.close()
        db.close()
        
        return {"message": f"Successfully created {table_name} and queued {total_rows} rows."}
        
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/uploads")
def get_uploads():
    try:
        db = get_db_connection()
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT id, filename, total_rows, table_name, created_at FROM uploads ORDER BY id DESC")
        uploads = cursor.fetchall()
        
        # For each upload, query its dynamic table for stats
        for u in uploads:
            tname = u['table_name']
            if not tname: continue
            
            try:
                cursor.execute(f"SELECT scrape_status, COUNT(*) as cnt FROM {tname} GROUP BY scrape_status")
                stats = cursor.fetchall()
                
                u['pending_count'] = 0
                u['processing_count'] = 0
                u['done_count'] = 0
                u['error_count'] = 0
                
                for s in stats:
                    st = s['scrape_status']
                    c = s['cnt']
                    if st == 'Pending': u['pending_count'] = c
                    elif st == 'Processing': u['processing_count'] = c
                    elif st == 'Done': u['done_count'] = c
                    elif st == 'Error': u['error_count'] = c
            except Exception as e:
                # Table might have been deleted manually
                pass
                
        cursor.close()
        db.close()
        return uploads
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/uploads/{upload_id}")
def delete_upload(upload_id: int):
    try:
        db = get_db_connection()
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT file_path, table_name FROM uploads WHERE id = %s", (upload_id,))
        record = cursor.fetchone()
        if not record:
            cursor.close()
            db.close()
            raise HTTPException(status_code=404, detail="Upload not found")
            
        file_path = record['file_path']
        if file_path != "stored_in_db" and os.path.exists(file_path):
            try: os.remove(file_path)
            except: pass
            
        tname = record['table_name']
        if tname:
            try:
                cursor.execute(f"DROP TABLE IF EXISTS {tname}")
            except: pass
            
            # Also clean up related data in main tables
            try:
                cursor.execute("DELETE FROM businesses WHERE batch_name = %s", (tname,))
                cursor.execute("DELETE FROM scrape_queue WHERE batch_name = %s", (tname,))
            except: pass
                
        cursor.execute("DELETE FROM uploads WHERE id = %s", (upload_id,))
        db.commit()
        cursor.close()
        db.close()
        return {"success": True, "message": "Upload deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/table/{table_name}")
def get_table_data(table_name: str, limit: int = 100, offset: int = 0):
    try:
        db = get_db_connection()
        cursor = db.cursor(dictionary=True)
        
        # Verify table exists to prevent SQL injection
        cursor.execute("SHOW TABLES LIKE %s", (table_name,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Table not found")
            
        cursor.execute(f"SELECT * FROM {table_name} LIMIT %s OFFSET %s", (limit, offset))
        data = cursor.fetchall()
        
        cursor.execute(f"SELECT COUNT(*) as count FROM {table_name}")
        total = cursor.fetchone()['count']
        
        cursor.close()
        db.close()
        
        return {"data": data, "total": total}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class OrchestratorRequest(BaseModel):
    table_name: str
    action: str # start or stop
    limit: int = 0

import subprocess

orchestrator_process = None

@app.post("/orchestrator")
def manage_orchestrator(req: OrchestratorRequest):
    global orchestrator_process
    if req.action == "start":
        if orchestrator_process and orchestrator_process.poll() is None:
            return {"message": "Orchestrator is already running!"}
            
        # Launch orchestrator
        python_exe = sys.executable
        orchestrator_process = subprocess.Popen([python_exe, "-u", "orchestrator.py", req.table_name, str(req.limit)])
        return {"message": f"Started 4 concurrent workers on table {req.table_name}"}
        
    elif req.action == "stop":
        if orchestrator_process and orchestrator_process.poll() is None:
            orchestrator_process.terminate()
            orchestrator_process = None
            
            # Reset processing to pending
            try:
                db = get_db_connection()
                cursor = db.cursor()
                cursor.execute(f"UPDATE {req.table_name} SET scrape_status = 'Pending' WHERE scrape_status = 'Processing'")
                db.commit()
                cursor.close()
                db.close()
            except: pass
            
            return {"message": "Stopped orchestrator and reset Processing rows to Pending."}
        else:
            return {"message": "Orchestrator is not running."}
