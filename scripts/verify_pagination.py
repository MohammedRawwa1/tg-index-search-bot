import asyncio
import uuid
from app.services.internal_pages import InMemoryInternalPageStore
from app.utils.helpers import md_to_markdown
from app.config.settings import settings

async def main():
    store = InMemoryInternalPageStore()
    group = str(uuid.uuid4())
    # Create 3 pages
    pages = []
    for i in range(3):
        content = "\n".join([f"{j+1+i*100}) [Title {j+1+i*100}](https://t.me/c/12345/{1000+j+1+i*100})" for j in range(100)])
        saved = await store.save_raw_page("test", content, [], created_by=None, group=group, part_index=i, total_parts=3, total_results=300)
        pages.append(saved.get("page_id"))
    # Fetch each page and build nav rows
    for pid in pages:
        p = await store.get_page(pid)
        gp = p.get("group_pages") or []
        total = len(gp)
        cur_idx = int(p.get("part_index") or 0)
        kb_rows = []
        # top links none
        nav = []
        if cur_idx > 0:
            nav.append({"text": "⬅️ Prev", "callback_data": f"IP|{gp[cur_idx-1]}"})
            nav.append({"text": "🏠 Home", "callback_data": f"IP|{gp[0]}"})
        nav.append({"text": f"Part {cur_idx+1}/{max(1,total)}", "callback_data": "noop"})
        if total > 0 and cur_idx < total - 1:
            nav.append({"text": "End ⏭", "callback_data": f"IP|{gp[-1]}"})
            nav.append({"text": "Next ➡️", "callback_data": f"IP|{gp[cur_idx+1]}"})
        if nav:
            kb_rows.append(nav)
        print(f"Page {cur_idx+1}/{total}: gp={gp}")
        print("Keyboard nav row:", kb_rows)

asyncio.run(main())
