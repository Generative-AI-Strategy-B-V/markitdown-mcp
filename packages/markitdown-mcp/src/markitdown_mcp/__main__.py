import contextlib
import sys
import os
import re
import json
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from typing import Literal
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from mcp.server.sse import SseServerTransport
from starlette.requests import Request
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from markitdown import MarkItDown
import uvicorn

# Initialize FastMCP server for MarkItDown (SSE)
mcp = FastMCP("markitdown")


@mcp.tool()
async def convert_to_markdown(uri: str) -> str:
    """Convert a resource described by an http:, https:, file: or data: URI to markdown"""
    return MarkItDown(enable_plugins=check_plugins_enabled()).convert_uri(uri).markdown


@mcp.tool()
async def convert_to_markdown_with_metadata(uri: str) -> str:
    """Convert a resource to markdown and include document metadata (name, size, dates, etc.)"""
    # Convert to markdown
    markdown_content = MarkItDown(enable_plugins=check_plugins_enabled()).convert_uri(uri).markdown
    
    # Try to get file metadata if it's a local file
    metadata_lines = ["# Document Metadata\n"]
    
    # Parse the URI
    parsed_uri = urllib.parse.urlparse(uri)
    
    if parsed_uri.scheme == "file":
        # Extract local file path
        local_path = _file_path_from_uri(uri)
        
        if local_path and os.path.exists(local_path):
            # Get file stats
            stat_info = os.stat(local_path)
            
            # Document name
            file_name = os.path.basename(local_path)
            metadata_lines.append(f"**Document Name:** {file_name}\n")
            
            # File size (in bytes and human-readable)
            file_size_bytes = stat_info.st_size
            file_size_kb = file_size_bytes / 1024
            file_size_mb = file_size_kb / 1024
            
            if file_size_mb >= 1:
                size_str = f"{file_size_mb:.2f} MB"
            elif file_size_kb >= 1:
                size_str = f"{file_size_kb:.2f} KB"
            else:
                size_str = f"{file_size_bytes} bytes"
            
            metadata_lines.append(f"**File Size:** {size_str} ({file_size_bytes:,} bytes)\n")
            
            # File extension/type
            file_ext = os.path.splitext(local_path)[1]
            if file_ext:
                metadata_lines.append(f"**File Type:** {file_ext}\n")
            
            # Full path
            metadata_lines.append(f"**Full Path:** `{local_path}`\n")
            
            # Creation time
            import datetime
            creation_time = datetime.datetime.fromtimestamp(stat_info.st_ctime)
            metadata_lines.append(f"**Created:** {creation_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            
            # Modification time
            modified_time = datetime.datetime.fromtimestamp(stat_info.st_mtime)
            metadata_lines.append(f"**Last Modified:** {modified_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            
            # Access time
            access_time = datetime.datetime.fromtimestamp(stat_info.st_atime)
            metadata_lines.append(f"**Last Accessed:** {access_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        else:
            metadata_lines.append(f"**Source URI:** {uri}\n")
            metadata_lines.append("*Note: File not found or inaccessible for metadata extraction*\n")
    elif parsed_uri.scheme in ("http", "https"):
        # For HTTP/HTTPS, we can only show the URL
        metadata_lines.append(f"**Source URL:** {uri}\n")
        metadata_lines.append(f"**Domain:** {parsed_uri.netloc}\n")
    else:
        # For other URI types
        metadata_lines.append(f"**Source URI:** {uri}\n")
        metadata_lines.append(f"**URI Scheme:** {parsed_uri.scheme}\n")
    
    # Combine metadata and content
    metadata_section = "".join(metadata_lines)
    separator = "\n" + "="*80 + "\n\n"
    
    return metadata_section + separator + markdown_content


@mcp.tool()
async def save_uri_as_markdown(uri: str, output_path: str) -> str:
    """Convert a resource (http/https/file/data URI) to Markdown and save it to output_path (.txt or .md)."""
    markdown_text = (
        MarkItDown(enable_plugins=check_plugins_enabled()).convert_uri(uri).markdown
    )
    # Ensure output directory exists
    abs_output_path = os.path.abspath(output_path)
    output_dir = os.path.dirname(abs_output_path)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    # Write UTF-8 with LF newlines for portability
    with open(abs_output_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(markdown_text)

    byte_len = len(markdown_text.encode("utf-8"))
    return f"Saved {byte_len} bytes to {abs_output_path}"


def _file_path_from_uri(uri: str) -> str | None:
    """Return local filesystem path from a file: URI. Windows-friendly."""
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "file":
        return None
    path = urllib.parse.unquote(parsed.path)
    # On Windows file:///C:/... becomes "/C:/..."; strip leading slash
    if os.name == "nt" and path.startswith("/") and len(path) > 3 and path[2] == ":":
        path = path[1:]
    return os.path.normpath(path)


@mcp.tool()
async def save_spreadsheet_with_formulas(
    uri: str,
    output_path: str,
    sheets: str | None = None,
) -> str:
    """Convert a spreadsheet to Markdown, appending a section that includes Excel formulas and cached values.

    - Supports file: URIs pointing to .xlsx/.xlsm/.xltx/.xltm
    - For non-spreadsheet URIs, saves normal Markdown only
    """
    md = MarkItDown(enable_plugins=check_plugins_enabled()).convert_uri(uri).markdown

    local_path = _file_path_from_uri(uri)
    formulas_section = ""

    if local_path and os.path.splitext(local_path)[1].lower() in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
        sections: list[str] = ["\n\n---\n\n## Formulas"]
        sheet_filter: set[str] | None = None
        if sheets:
            sheet_filter = {s.strip() for s in sheets.split(",") if s.strip()}

        # Stream-parse the .xlsx to extract formulas and cached values without loading full sheets
        with zipfile.ZipFile(local_path, "r") as zf:
            # Map sheet name -> xml path
            name_to_xml = _map_sheet_names_to_xml_paths(zf)

            # Load shared strings once (for string cached results)
            shared_strings = _load_shared_strings(zf)

            for sheet_name, xml_path in name_to_xml.items():
                if sheet_filter and sheet_name not in sheet_filter:
                    continue

                lines: list[str] = []
                lines.append("| Cell | Formula | Cached Value |")
                lines.append("| --- | --- | --- |")

                for coord, formula, cached_val in _iter_sheet_formulas(zf, xml_path, shared_strings):
                    ft = (formula or "").replace("|", "\\|")
                    cv = ("" if cached_val is None else str(cached_val)).replace("|", "\\|")
                    lines.append(f"| {coord} | `{ft}` | {cv} |")

                if len(lines) > 2:
                    sections.append(f"\n### Sheet: {sheet_name}\n" + "\n".join(lines))

        if len(sections) > 1:
            formulas_section = "".join(sections)

    final_markdown = md + formulas_section

    # Ensure output directory exists and write
    abs_output_path = os.path.abspath(output_path)
    output_dir = os.path.dirname(abs_output_path)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    with open(abs_output_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(final_markdown)

    return f"Saved {len(final_markdown.encode('utf-8'))} bytes to {abs_output_path}"


def check_plugins_enabled() -> bool:
    return os.getenv("MARKITDOWN_ENABLE_PLUGINS", "false").strip().lower() in (
        "true",
        "1",
        "yes",
    )


# ============================================================================
# AI-Optimized Excel Conversion
# ============================================================================
# The standard markdown output from Excel files contains ~60% NaN cells and
# uses a confusing label+value column pair format. These functions provide
# token-efficient formats optimized for AI agent consumption.


@mcp.tool()
async def convert_excel_to_ai_format(
    uri: str,
    output_format: Literal["flat", "sparse", "json"] = "flat",
    sheets: str | None = None,
    skip_empty: bool = True,
    normalize_dates: bool = True,
) -> str:
    """Convert Excel spreadsheet to AI-optimized format. Removes NaN cells, normalizes dates.

    Args:
        uri: file: URI to .xlsx/.xls file
        output_format: "flat" (grep-friendly), "sparse" (50% smaller), or "json" (structured)
        sheets: Comma-separated sheet names to include (default: all)
        skip_empty: Skip cells with NaN/empty values (default: True)
        normalize_dates: Convert dates to YYYY-MM format (default: True)

    Returns:
        Token-efficient representation of the spreadsheet data.
        - flat: "CATEGORY > LABEL | MONTH | VALUE" per line, easy to grep
        - sparse: Row definitions + Month index + Values only, ~50% smaller
        - json: Nested structure for programmatic access

    Example queries with flat format:
        grep "Maurice Fixed" output.txt | grep "2026-01"
    """
    local_path = _file_path_from_uri(uri)
    if not local_path:
        return "Error: Only file: URIs are supported for Excel conversion"

    ext = os.path.splitext(local_path)[1].lower()
    if ext not in {".xlsx", ".xls", ".xlsm", ".xltx", ".xltm"}:
        return f"Error: Unsupported file type: {ext}. Expected .xlsx or .xls"

    if not os.path.exists(local_path):
        return f"Error: File not found: {local_path}"

    # Parse the spreadsheet
    sheet_filter: set[str] | None = None
    if sheets:
        sheet_filter = {s.strip() for s in sheets.split(",") if s.strip()}

    data = _parse_excel_to_structured(local_path, sheet_filter, skip_empty, normalize_dates)

    if output_format == "flat":
        return _format_as_flat(data)
    elif output_format == "sparse":
        return _format_as_sparse(data)
    elif output_format == "json":
        return json.dumps(data, indent=2, ensure_ascii=False)
    else:
        return f"Error: Unknown format: {output_format}"


@mcp.tool()
async def save_excel_as_ai_format(
    uri: str,
    output_path: str,
    output_format: Literal["flat", "sparse", "json"] = "flat",
    sheets: str | None = None,
    skip_empty: bool = True,
    normalize_dates: bool = True,
) -> str:
    """Convert Excel to AI-optimized format and save to file. Returns file path and size.

    Args:
        uri: file: URI to .xlsx/.xls file
        output_path: Where to save the output (.txt, .json, or .md)
        output_format: "flat" (grep-friendly), "sparse" (50% smaller), or "json"
        sheets: Comma-separated sheet names to include (default: all)
        skip_empty: Skip cells with NaN/empty values (default: True)
        normalize_dates: Convert dates to YYYY-MM format (default: True)

    TOKEN SAVING: Use sparse format for ~50% size reduction vs markdown tables.
    """
    content = await convert_excel_to_ai_format(
        uri, output_format, sheets, skip_empty, normalize_dates
    )

    if content.startswith("Error:"):
        return content

    abs_output_path = os.path.abspath(output_path)
    output_dir = os.path.dirname(abs_output_path)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    with open(abs_output_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)

    byte_len = len(content.encode("utf-8"))
    return f"Saved {byte_len:,} bytes to {abs_output_path} (format: {output_format})"


def _parse_excel_to_structured(
    local_path: str,
    sheet_filter: set[str] | None,
    skip_empty: bool,
    normalize_dates: bool,
) -> dict:
    """Parse Excel file into structured data format."""
    try:
        import pandas as pd
    except ImportError:
        return {"error": "pandas not installed"}

    try:
        import openpyxl  # noqa: F401
        engine = "openpyxl"
    except ImportError:
        try:
            import xlrd  # noqa: F401
            engine = "xlrd"
        except ImportError:
            return {"error": "Neither openpyxl nor xlrd installed"}

    try:
        all_sheets = pd.read_excel(local_path, sheet_name=None, engine=engine, header=None)
    except Exception as e:
        return {"error": f"Failed to read Excel: {str(e)}"}

    result = {
        "metadata": {
            "source": os.path.basename(local_path),
            "sheets": [],
            "months": set(),
        },
        "data": [],
    }

    for sheet_name, df in all_sheets.items():
        if sheet_filter and sheet_name not in sheet_filter:
            continue

        result["metadata"]["sheets"].append(sheet_name)

        # Process each row
        for row_idx in range(len(df)):
            row = df.iloc[row_idx]

            # Build category path from leading non-empty, non-numeric cells
            category_path = []
            data_start_col = 0

            for col_idx, cell in enumerate(row):
                if _is_empty(cell):
                    continue
                if _is_numeric(cell):
                    data_start_col = col_idx
                    break
                # Check if it looks like a date
                if normalize_dates:
                    month = _normalize_month(str(cell))
                    if month:
                        data_start_col = col_idx
                        break
                category_path.append(str(cell).strip())
                data_start_col = col_idx + 1

            if not category_path:
                continue

            # Extract month/value pairs from remaining columns
            # Handle the label+value pair format
            col = data_start_col
            while col < len(row):
                cell = row.iloc[col]

                if _is_empty(cell):
                    col += 1
                    continue

                # Check if this cell is a date/month
                month = None
                if normalize_dates:
                    month = _normalize_month(str(cell))

                if month:
                    result["metadata"]["months"].add(month)
                    # Next cell might be the value
                    if col + 1 < len(row):
                        next_cell = row.iloc[col + 1]
                        if _is_numeric(next_cell):
                            value = float(next_cell)
                            if not skip_empty or value != 0:
                                result["data"].append({
                                    "sheet": sheet_name,
                                    "category": " > ".join(category_path),
                                    "month": month,
                                    "value": value,
                                })
                            col += 2
                            continue
                    col += 1
                elif _is_numeric(cell):
                    # Numeric value without clear month association
                    value = float(cell)
                    if not skip_empty or value != 0:
                        result["data"].append({
                            "sheet": sheet_name,
                            "category": " > ".join(category_path),
                            "month": None,
                            "value": value,
                            "col_index": col,
                        })
                    col += 1
                else:
                    # Text label - might be part of a label+value pair
                    label = str(cell).strip()
                    if col + 1 < len(row):
                        next_cell = row.iloc[col + 1]
                        if _is_numeric(next_cell):
                            value = float(next_cell)
                            if not skip_empty or value != 0:
                                full_category = " > ".join(category_path + [label])
                                result["data"].append({
                                    "sheet": sheet_name,
                                    "category": full_category,
                                    "month": None,
                                    "value": value,
                                    "col_index": col,
                                })
                            col += 2
                            continue
                    col += 1

    # Convert months set to sorted list
    result["metadata"]["months"] = sorted(result["metadata"]["months"])
    result["metadata"]["row_count"] = len(result["data"])

    return result


def _format_as_flat(data: dict) -> str:
    """Format data as grep-friendly flat text."""
    if "error" in data:
        return f"Error: {data['error']}"

    lines = [
        "# EXCEL DATA - AI-Optimized Flat Format",
        f"# Source: {data['metadata']['source']}",
        f"# Sheets: {', '.join(data['metadata']['sheets'])}",
        f"# Rows: {data['metadata']['row_count']}",
        "# Format: CATEGORY | MONTH | VALUE",
        "# Query: grep \"search term\" file.txt | grep \"2026-01\"",
        "",
    ]

    for item in data["data"]:
        month = item.get("month") or f"col_{item.get('col_index', '?')}"
        lines.append(f"{item['category']} | {month} | {item['value']}")

    return "\n".join(lines)


def _format_as_sparse(data: dict) -> str:
    """Format data as sparse matrix (row IDs + month IDs + values only)."""
    if "error" in data:
        return f"Error: {data['error']}"

    lines = [
        "# EXCEL DATA - Sparse Matrix Format",
        f"# Source: {data['metadata']['source']}",
        "# ~50% smaller than flat format",
        "",
        "## ROWS",
    ]

    # Build unique row paths
    row_paths: dict[str, str] = {}
    row_id = 1
    for item in data["data"]:
        cat = item["category"]
        if cat not in row_paths:
            rid = f"R{str(row_id).zfill(4)}"
            row_paths[cat] = rid
            lines.append(f"{rid}: {cat}")
            row_id += 1

    lines.append("")
    lines.append("## MONTHS")

    # Build month index
    month_index: dict[str, str] = {}
    for i, month in enumerate(data["metadata"]["months"]):
        mid = f"M{str(i + 1).zfill(2)}"
        month_index[month] = mid
        lines.append(f"{mid}: {month}")

    lines.append("")
    lines.append("## VALUES")

    for item in data["data"]:
        rid = row_paths[item["category"]]
        month = item.get("month")
        if month and month in month_index:
            mid = month_index[month]
            lines.append(f"{rid},{mid}: {item['value']}")
        else:
            # No month, use column index
            col = item.get("col_index", "?")
            lines.append(f"{rid},C{col}: {item['value']}")

    return "\n".join(lines)


def _is_empty(val) -> bool:
    """Check if value is empty/NaN."""
    if val is None:
        return True
    if isinstance(val, float) and (val != val):  # NaN check
        return True
    s = str(val).strip().lower()
    return s in ("", "nan", "none", "null", "---")


def _is_numeric(val) -> bool:
    """Check if value is numeric."""
    if val is None:
        return False
    if isinstance(val, (int, float)):
        if isinstance(val, float) and (val != val):  # NaN
            return False
        return True
    try:
        float(str(val).replace(",", ""))
        return True
    except (ValueError, TypeError):
        return False


def _normalize_month(date_str: str) -> str | None:
    """Normalize date string to YYYY-MM format."""
    if not date_str or date_str.lower() in ("nan", "none", "null"):
        return None

    date_str = date_str.strip()

    # Handle ISO format: "2024-01-01 00:00:00" or "2024-01-01"
    iso_match = re.match(r"^(\d{4})-(\d{2})-\d{2}", date_str)
    if iso_match:
        return f"{iso_match.group(1)}-{iso_match.group(2)}"

    # Handle Dutch/English short format: "Okt-24", "Nov-24", "Jan-25"
    dutch_months = {
        "jan": "01", "feb": "02", "mar": "03", "mrt": "03",
        "apr": "04", "may": "05", "mei": "05", "jun": "06",
        "jul": "07", "aug": "08", "sep": "09", "okt": "10",
        "oct": "10", "nov": "11", "dec": "12",
    }

    short_match = re.match(r"^([A-Za-z]{3})-(\d{2})$", date_str)
    if short_match:
        month_abbr = short_match.group(1).lower()
        year_short = short_match.group(2)
        if month_abbr in dutch_months:
            return f"20{year_short}-{dutch_months[month_abbr]}"

    # Handle format: "January 2024" or "Jan 2024"
    full_match = re.match(r"^([A-Za-z]+)\s*(\d{4})$", date_str)
    if full_match:
        month_name = full_match.group(1).lower()[:3]
        year = full_match.group(2)
        if month_name in dutch_months:
            return f"{year}-{dutch_months[month_name]}"

    return None


# XML namespaces used in .xlsx files
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _map_sheet_names_to_xml_paths(zf: zipfile.ZipFile) -> dict[str, str]:
    """Return mapping of sheet name -> zip path for that sheet's XML."""
    result: dict[str, str] = {}
    try:
        with zf.open("xl/workbook.xml") as f:
            tree = ET.parse(f)
    except KeyError:
        return result

    root = tree.getroot()
    sheets = root.find(f"{{{MAIN_NS}}}sheets")
    if sheets is None:
        return result

    # Build rId -> Target from relationships
    rels: dict[str, str] = {}
    try:
        with zf.open("xl/_rels/workbook.xml.rels") as rf:
            rtree = ET.parse(rf)
            rroot = rtree.getroot()
            for rel in rroot.findall(f"{{{PKG_REL_NS}}}Relationship"):
                rid = rel.attrib.get("Id")
                target = rel.attrib.get("Target")
                if rid and target:
                    # Normalize path under xl/
                    target_path = target.replace("\\", "/")
                    if not target_path.startswith("/"):
                        target_path = f"xl/{target_path}"
                    else:
                        target_path = target_path.lstrip("/")
                    rels[rid] = target_path
    except KeyError:
        pass

    for sheet in sheets.findall(f"{{{MAIN_NS}}}sheet"):
        name = sheet.attrib.get("name")
        rid = sheet.attrib.get(f"{{{OFFICE_REL_NS}}}id")
        if not name or not rid:
            continue
        target = rels.get(rid)
        if target:
            result[name] = target
    return result


def _load_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    """Parse sharedStrings.xml into a list of strings (by index)."""
    strings: list[str] = []
    try:
        with zf.open("xl/sharedStrings.xml") as f:
            # iterparse to reduce memory
            for event, elem in ET.iterparse(f, events=("end",)):
                if elem.tag == f"{{{MAIN_NS}}}si":
                    text = "".join(elem.itertext())
                    strings.append(text)
                    elem.clear()
    except KeyError:
        pass
    return strings


def _decode_cell_value(value_text: str | None, t_attr: str | None, shared_strings: list[str]) -> str | None:
    if value_text is None:
        return None
    if t_attr == "s":
        try:
            idx = int(float(value_text))
            if 0 <= idx < len(shared_strings):
                return shared_strings[idx]
        except Exception:
            return value_text
    # boolean
    if t_attr == "b":
        return "TRUE" if value_text in ("1", "true", "True") else "FALSE"
    # inline string or formula string
    if t_attr in ("str", "inlineStr"):
        return value_text
    # default: numeric or general
    return value_text


def _iter_sheet_formulas(
    zf: zipfile.ZipFile, xml_path: str, shared_strings: list[str]
) -> tuple[str, str, str | None]:
    """Yield (cell_ref, formula_text, cached_value) for each formula cell in sheet XML.

    This uses a streaming XML parser to avoid loading entire sheets.
    """
    try:
        f = zf.open(xml_path)
    except KeyError:
        return iter(())  # type: ignore

    def gen():
        current_ref: str | None = None
        current_type: str | None = None
        current_formula: str | None = None
        current_value: str | None = None
        for event, elem in ET.iterparse(f, events=("start", "end")):
            if event == "start" and elem.tag == f"{{{MAIN_NS}}}c":
                current_ref = elem.attrib.get("r")
                current_type = elem.attrib.get("t")
                current_formula = None
                current_value = None
            elif event == "end":
                if elem.tag == f"{{{MAIN_NS}}}f":
                    # Formula text
                    txt = elem.text or ""
                    if txt and not txt.startswith("="):
                        txt = "=" + txt
                    current_formula = txt
                    elem.clear()
                elif elem.tag == f"{{{MAIN_NS}}}v":
                    current_value = elem.text
                    elem.clear()
                elif elem.tag == f"{{{MAIN_NS}}}c":
                    if current_ref and current_formula is not None:
                        yield (
                            current_ref,
                            current_formula,
                            _decode_cell_value(current_value, current_type, shared_strings),
                        )
                    elem.clear()
                    current_ref = None
        try:
            f.close()
        except Exception:
            pass

    return gen()


def create_starlette_app(mcp_server: Server, *, debug: bool = False) -> Starlette:
    sse = SseServerTransport("/messages/")
    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        event_store=None,
        json_response=True,
        stateless=True,
    )

    async def handle_sse(request: Request) -> None:
        async with sse.connect_sse(
            request.scope,
            request.receive,
            request._send,
        ) as (read_stream, write_stream):
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
            )

    async def handle_streamable_http(
        scope: Scope, receive: Receive, send: Send
    ) -> None:
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        """Context manager for session manager."""
        async with session_manager.run():
            print("Application started with StreamableHTTP session manager!")
            try:
                yield
            finally:
                print("Application shutting down...")

    return Starlette(
        debug=debug,
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/mcp", app=handle_streamable_http),
            Mount("/messages/", app=sse.handle_post_message),
        ],
        lifespan=lifespan,
    )


# Main entry point
def main():
    import argparse

    mcp_server = mcp._mcp_server

    parser = argparse.ArgumentParser(description="Run a MarkItDown MCP server")

    parser.add_argument(
        "--http",
        action="store_true",
        help="Run the server with Streamable HTTP and SSE transport rather than STDIO (default: False)",
    )
    parser.add_argument(
        "--sse",
        action="store_true",
        help="(Deprecated) An alias for --http (default: False)",
    )
    parser.add_argument(
        "--host", default=None, help="Host to bind to (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=None, help="Port to listen on (default: 3001)"
    )
    args = parser.parse_args()

    use_http = args.http or args.sse

    if not use_http and (args.host or args.port):
        parser.error(
            "Host and port arguments are only valid when using streamable HTTP or SSE transport (see: --http)."
        )
        sys.exit(1)

    if use_http:
        starlette_app = create_starlette_app(mcp_server, debug=True)
        uvicorn.run(
            starlette_app,
            host=args.host if args.host else "127.0.0.1",
            port=args.port if args.port else 3001,
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
