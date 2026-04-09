#!/usr/bin/env python3
import asyncio
import os
import sys

try:
    from app.api import _ensure_internal_page_store
except Exception as e:
    print('Failed to import _ensure_internal_page_store:', e)
    raise

async def main():
    store = _ensure_internal_page_store()
    if not store:
        print('No store available')
        return
    lines = [f"{i}) [Item](https://t.me/c/12345/{1000+i})" for i in range(1, 350)]
    md = "\n".join(lines)
    print('Length of md:', len(md), 'lines:', len(lines))
    try:
        saved = await store.save_raw_page('autosplit_test', md, tokens=[], created_by=None)
        print('Saved page info:', saved)
        page = await store.get_page(saved.get('page_id'))
        print('Loaded page:', page.get('page_id'), 'part_index:', page.get('part_index'), 'total_parts:', page.get('total_parts'), 'total_results:', page.get('total_results'))
        gp = page.get('group_pages') or []
        print('Group pages count:', len(gp))
    except Exception as e:
        print('save_raw_page failed:', e)

if __name__ == '__main__':
    asyncio.run(main())
