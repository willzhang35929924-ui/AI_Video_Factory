"""Observed TikTok Studio uploads in an already-open, user-owned HubStudio browser."""
import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout, Error as BrowserError


def attachment_binding(payload):
    account, match = payload['account'], payload['product_match']
    return {'publishing_task_id': payload['publishing_task']['id'], 'product_id': payload['product']['id'],
        'account_id': account['id'], 'environment_id': account['hubstudio_env_id'],
        'cdp_endpoint': account['cdp_endpoint'], 'username': account['expected_username'], 'country': account['country'],
        'video_sha256': payload['video']['sha256'],
        'caption_sha256': hashlib.sha256(payload['publishing_task']['caption'].encode()).hexdigest(),
        'mode': match['mode'], 'expected_names': match['expected_names'], 'requested_id': match['requested_id']}


def submission_binding(payload):
    account, row = payload['account'], payload['publishing_task']
    return {'publishing_task_id': row['id'], 'account_id': account['id'],
        'environment_id': account['hubstudio_env_id'], 'cdp_endpoint': account['cdp_endpoint'],
        'username': account['expected_username'], 'country': account['country'],
        'video_sha256': payload['video']['sha256'],
        'description_sha256': hashlib.sha256(row['caption'].encode()).hexdigest(),
        'selected_product_id': payload.get('checkpoint', {}).get('ATTACH_PRODUCT', {}).get('checkpoint', {}).get('selected_id')}


def studio_item_matches(item, payload):
    intent = payload.get('checkpoint', {}).get('submission_intent', {})
    try:
        identifier = item['item_id']
        created, posted = int(item['create_time']), int(item['post_time'])
        started = datetime.fromisoformat(intent['started_at'].replace('Z', '+00:00')).timestamp()
        duration = float(payload['video']['duration']) * 1000
        parsed = urlsplit(item['href'])
        return (isinstance(identifier, str) and identifier.isdigit() and item['desc'] == payload['publishing_task']['caption']
            and created == posted and started <= created <= datetime.now(timezone.utc).timestamp()
            and duration > 0 and abs(float(item['duration']) - duration) <= 100
            and parsed.scheme == 'https' and parsed.hostname == 'www.tiktok.com' and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment and parsed.path == f"/@{intent['username']}/video/{identifier}")
    except (ValueError, TypeError, KeyError):
        return False


def studio_readback(config, payload, reference):
    try:
        directory = config.output_root / payload['publishing_task']['id']
        path = Path(reference['path'])
        if directory.resolve() != directory or path.parent != directory or path.resolve() != path or not path.is_file() or path.stat().st_size > 65536:
            return None
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference['sha256']:
            return None
        receipt = json.loads(raw)
        intent = payload['checkpoint']['submission_intent']
        intent_hash = hashlib.sha256(json.dumps(intent, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if (receipt.get('source') != 'OBSERVED_STUDIO_POST_READBACK' or receipt.get('binding') != submission_binding(payload)
                or receipt.get('submission_intent_sha256') != intent_hash or not studio_item_matches(receipt.get('item', {}), payload)):
            return None
        for name in ('UPLOAD_VIDEO', 'FILL_DESCRIPTION', 'ATTACH_PRODUCT', 'SUBMIT'):
            result = payload['checkpoint'].get(name)
            if not result or receipt.get('step_sha256', {}).get(name) != hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest():
                return None
        screenshot = Path(receipt['screenshot'])
        if (screenshot.parent != directory or screenshot.resolve() != screenshot or not screenshot.is_file()
                or hashlib.sha256(screenshot.read_bytes()).hexdigest() != receipt['screenshot_sha256']):
            return None
        created = datetime.fromisoformat(receipt['created_at'])
        if not datetime.fromisoformat(intent['started_at'].replace('Z', '+00:00')) <= created <= datetime.now(timezone.utc):
            return None
        return {'path': str(path), 'sha256': reference['sha256'], 'receipt': receipt}
    except (OSError, ValueError, TypeError, KeyError):
        return None


def product_receipt(config, payload, reference=None):
    directory = config.output_root / payload['publishing_task']['id']
    if config.output_root.resolve() != config.output_root or directory.resolve() != directory or directory.parent != config.output_root:
        return None
    paths = [Path(reference.get('path', ''))] if reference else list(directory.glob('product-confirmation-*.json'))
    matches = []
    for path in paths:
        try:
            if path.parent != directory or path.resolve() != path or not path.is_file() or path.stat().st_size > 65536:
                continue
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if reference and reference.get('sha256') != digest:
                continue
            receipt = json.loads(raw)
            source = receipt.get('source')
            attempt = payload.get('checkpoint', {}).get('product_reconciliation_attempt' if source == 'OBSERVED_ATTACHED_PRODUCT_READBACK' else 'product_attachment_attempt')
            if (source not in {'OBSERVED_PRODUCT_SELECTION_BEFORE_CONFIRM', 'OBSERVED_ATTACHED_PRODUCT_READBACK'}
                    or not attempt or not all(attempt.get(key) for key in ('run_id', 'step_id', 'started_at'))
                    or receipt.get('binding') != attachment_binding(payload) or receipt.get('attempt') != attempt):
                continue
            title, sku, label = receipt.get('selected_name'), receipt.get('selected_id'), receipt.get('display_name')
            if any(not isinstance(value, str) or not value.strip() for value in (title, sku, label)) or not title.startswith(label):
                continue
            if source == 'OBSERVED_ATTACHED_PRODUCT_READBACK':
                observation = payload.get('checkpoint', {}).get('product_observation', {})
                catalog_at = datetime.fromisoformat(observation.get('evidence', {}).get('verified_at', ''))
                if catalog_at < datetime.fromisoformat(attempt['started_at']):
                    continue
                observation_hash = hashlib.sha256(json.dumps(observation, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                metadata = receipt.get('anchor_metadata', {})
                if (receipt.get('catalog_observation_sha256') != observation_hash or metadata != {'type': 33, 'product_id': sku, 'keyword': label}
                        or not any(item.get('product_name') == title and item.get('product_id') == sku and item.get('matches') is True
                            for item in observation.get('evidence', {}).get('candidates', []))):
                    continue
            screenshot = Path(receipt.get('confirmation_screenshot', ''))
            if screenshot.parent != directory or screenshot.resolve() != screenshot or not screenshot.is_file() or hashlib.sha256(screenshot.read_bytes()).hexdigest() != receipt.get('confirmation_screenshot_sha256'):
                continue
            created = datetime.fromisoformat(receipt['created_at'])
            started = datetime.fromisoformat(attempt['started_at'])
            if created < started or created > datetime.now(timezone.utc):
                continue
            matches.append({'path': str(path), 'sha256': digest, 'receipt': receipt})
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return matches[0] if len(matches) == 1 else None


class ExistingHubBrowserAdapter:
    def __init__(self, config):
        self.config = config

    @staticmethod
    def upload_page(browser):
        pages = [page for context in browser.contexts for page in context.pages
                 if urlsplit(page.url).scheme == "https" and urlsplit(page.url).hostname == "www.tiktok.com"
                 and urlsplit(page.url).path == "/tiktokstudio/upload"
                 and not urlsplit(page.url).username and not urlsplit(page.url).password]
        return pages[0] if len(pages) == 1 else None

    def screenshot_path(self, task_id, label):
        root = self.config.output_root.resolve()
        if root != self.config.output_root:
            raise ValueError("PUBLISH_OUTPUT_PATH_NOT_ALLOWED")
        directory = root / task_id
        if directory.parent.resolve() != root or directory.resolve() != directory:
            raise ValueError("PUBLISH_OUTPUT_PATH_NOT_ALLOWED")
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"{label}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}.png"
        path = directory / filename
        if path.resolve().parent != directory:
            raise ValueError("PUBLISH_OUTPUT_PATH_NOT_ALLOWED")
        return path

    @staticmethod
    def caption(payload):
        row = payload['publishing_task']
        selected = row.get('selected_description') or {}
        text = row.get('caption', '')
        digest = hashlib.sha256(text.encode()).hexdigest()
        return text if text.strip() and len(text) <= 4000 and selected.get('text') == text and selected.get('sha256') == digest else None

    @staticmethod
    async def uploaded(page, payload):
        filename = Path(payload['video']['path']).name
        block = page.locator('.info-main').filter(has=page.get_by_text(filename, exact=True))
        completion = block.locator('.info-status.success')
        if (await block.count() != 1 or await completion.count() != 1 or not await completion.is_visible()
                or await completion.locator('[data-testid="CheckCircleFill"]').count() != 1):
            return False
        return bool(re.fullmatch(r'Cargado[（(]\d+(?:\.\d+)?(?:KB|MB|GB)[）)]', (await completion.inner_text()).strip()))

    async def evidence(self, page, payload, label, **values):
        screenshot = self.screenshot_path(payload['publishing_task']['id'], label)
        await page.screenshot(path=str(screenshot))
        account = payload['account']
        return {'source': 'OBSERVED_BROWSER', 'account_id': account['id'],
            'environment_id': account['hubstudio_env_id'], 'username': account['expected_username'],
            'video_sha256': payload['video']['sha256'], 'verified_at': datetime.now(timezone.utc).isoformat(),
            'url': page.url, 'screenshot': str(screenshot), 'publish_clicked': False, **values}

    @staticmethod
    def attached_product(page, label):
        return page.locator('[data-e2e="anchor_container"] .anchor-tag-label').get_by_text(label, exact=True)

    def save_product_receipt(self, payload, confirmation, title, product_id, label):
        screenshot = Path(confirmation['screenshot'])
        path = screenshot.with_suffix('.json')
        expected = self.config.output_root / payload['publishing_task']['id']
        if path.parent != expected or expected.resolve() != expected or self.config.output_root.resolve() != self.config.output_root or screenshot.resolve() != screenshot:
            raise ValueError('PUBLISH_OUTPUT_PATH_NOT_ALLOWED')
        receipt = {'source': 'OBSERVED_PRODUCT_SELECTION_BEFORE_CONFIRM', 'binding': attachment_binding(payload),
            'attempt': payload['checkpoint']['product_attachment_attempt'], 'created_at': datetime.now(timezone.utc).isoformat(),
            'selected_name': title, 'selected_id': product_id, 'display_name': label,
            'confirmation_screenshot': str(screenshot), 'confirmation_screenshot_sha256': hashlib.sha256(screenshot.read_bytes()).hexdigest()}
        raw = json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode()
        with path.open('xb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}

    async def attached_metadata(self, page):
        container = page.locator('[data-e2e="anchor_container"] .anchor-tag-container')
        if await container.count() != 1 or not await container.is_visible():
            return None
        return await container.evaluate("""node => {
            const key = Object.keys(node).find(key => key.startsWith('__reactFiber$'));
            const parent = key ? node[key]?.return : null;
            const values = [parent?.memoizedProps?.value, parent?.alternate?.memoizedProps?.value]
                .filter(value => Array.isArray(value) && value.length);
            const candidates = values.map(value => value.map(item => ({type: item?.type,
                keyword: item?.keyword, content: {keyword: item?.content?.keyword, product_id: item?.content?.product_id}})));
            if (!candidates.length || candidates.some(value => JSON.stringify(value) !== JSON.stringify(candidates[0]))) return null;
            const entries = candidates[0];
            if (entries.length !== 1 || entries[0].type !== 33 || typeof entries[0].content.product_id !== 'string'
                || entries[0].keyword !== entries[0].content.keyword) return null;
            return {type: 33, product_id: entries[0].content.product_id, keyword: entries[0].keyword};
        }""")

    async def save_product_readback(self, page, payload, label):
        metadata = await self.attached_metadata(page)
        observation = payload.get('checkpoint', {}).get('product_observation', {})
        if observation.get('status') != 'READY' or not metadata or metadata.get('keyword') != label:
            return None
        candidates = [item for item in observation.get('evidence', {}).get('candidates', [])
            if item.get('product_id') == metadata.get('product_id') and item.get('matches') is True
            and item.get('selectable') is True and item.get('product_name', '').startswith(label)]
        if len(candidates) != 1:
            return None
        item = candidates[0]
        evidence = await self.evidence(page, payload, 'product-readback', read_only=True)
        screenshot = Path(evidence['screenshot'])
        receipt = {'source': 'OBSERVED_ATTACHED_PRODUCT_READBACK', 'binding': attachment_binding(payload),
            'attempt': payload['checkpoint']['product_reconciliation_attempt'], 'created_at': datetime.now(timezone.utc).isoformat(),
            'selected_name': item['product_name'], 'selected_id': item['product_id'], 'display_name': label,
            'anchor_metadata': metadata, 'catalog_observation_sha256': hashlib.sha256(json.dumps(observation, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'confirmation_screenshot': str(screenshot), 'confirmation_screenshot_sha256': hashlib.sha256(screenshot.read_bytes()).hexdigest()}
        raw = json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode()
        path = screenshot.with_suffix('.json')
        with path.open('xb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(), 'receipt': receipt}

    async def fill_description(self, page, payload):
        text = self.caption(payload)
        if text is None:
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PERSISTED_DESCRIPTION_REQUIRED'}
        control = page.locator('[data-e2e="caption_container"] [contenteditable="true"][role="combobox"]')
        if await control.count() != 1 or not await control.is_visible():
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'CAPTION_CONTROL_CHANGED'}
        current = await control.inner_text()
        prior = payload.get('checkpoint', {}).get('FILL_DESCRIPTION', {}).get('checkpoint', {}).get('caption_sha256')
        known = current == text or current == Path(payload['video']['path']).stem or not current.strip()
        known = known or prior == hashlib.sha256(current.encode()).hexdigest()
        if not known:
            evidence = await self.evidence(page, payload, 'caption-existing-draft', read_only=True)
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'EXISTING_CAPTION_DO_NOT_OVERWRITE', 'evidence': evidence}
        changed = current != text
        if changed:
            await control.fill(text)
        await control.filter(has_text=text).wait_for(state='visible', timeout=5000)
        observed = await control.inner_text()
        digest = hashlib.sha256(text.encode()).hexdigest()
        evidence = await self.evidence(page, payload, 'caption-filled', read_only=not changed, caption_sha256=digest,
            dom_evidence={'caption': observed, 'url': page.url})
        proof = {'account_id': payload['account']['id'], 'environment_id': payload['account']['hubstudio_env_id'],
            'video_sha256': payload['video']['sha256'], 'caption_sha256': digest,
            'description_id': payload['publishing_task']['selected_description']['id']}
        return {'status': 'READY' if observed == text else 'WAITING_MANUAL', 'performed': changed,
            'checkpoint': proof, 'evidence': evidence, **({} if observed == text else {'error_code': 'CAPTION_READBACK_MISMATCH'})}

    async def attach_product(self, page, payload):
        text = self.caption(payload)
        caption = page.locator('[data-e2e="caption_container"] [contenteditable="true"][role="combobox"]')
        if (payload.get('checkpoint', {}).get('FILL_DESCRIPTION', {}).get('status') != 'READY' or text is None
                or await caption.count() != 1 or await caption.inner_text() != text):
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'VERIFIED_CAPTION_REQUIRED'}
        products = page.get_by_role('dialog', name='Añade enlaces de productos', exact=True)
        link = page.get_by_role('dialog', name='Añade un enlace', exact=True)
        if await products.is_visible() or await link.is_visible():
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'EXISTING_PRODUCT_DIALOG_DO_NOT_OVERWRITE'}
        submitted = False
        opened = False
        receipt = None
        try:
            add = page.get_by_role('button', name='Añadir', exact=True)
            if await add.count() != 1 or not await add.is_visible():
                return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'EXISTING_ATTACHMENT_REQUIRES_RECONCILIATION'}
            await add.click(timeout=10000)
            opened = True
            if (await link.get_by_role('combobox').inner_text()).strip() != 'Productos':
                return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PRODUCT_LINK_TYPE_NOT_SELECTED'}
            await link.get_by_role('button', name='Siguiente', exact=True).click(timeout=10000)
            await products.get_by_role('radio').first.wait_for(state='visible', timeout=10000)
            headers = [value.strip() for value in await products.locator('tr').first.locator('th').all_text_contents()]
            if headers != ['Nombre del producto', 'ID del producto', 'Precio', 'Existencias', '', 'Estado']:
                return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PRODUCT_TABLE_SCHEMA_CHANGED'}
            match = payload['product_match']
            names = [' '.join(name.casefold().split()) for name in match['expected_names']]
            candidates = []
            for row in await products.locator('tr').all():
                radio = row.get_by_role('radio')
                if await radio.count() != 1 or not await row.is_visible() or not await radio.is_visible():
                    continue
                cells = await row.locator('td').all_text_contents()
                title = (await radio.get_attribute('value') or '').strip()
                if len(cells) != 6 or not title or not cells[1].strip():
                    return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PRODUCT_ROW_INCOMPLETE'}
                product_id = cells[1].strip()
                name = ' '.join(title.casefold().split())
                matches = product_id == match['requested_id'] if match['mode'] == 'exact_id' else any(name == expected or name.startswith(expected + ' ') for expected in names)
                if matches and cells[-1].strip() == 'Activo' and await radio.is_enabled():
                    candidates.append((radio, title, product_id))
            if len(candidates) != 1:
                evidence = await self.evidence(page, payload, 'product-match-pending', read_only=True, match_count=len(candidates))
                return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'UNIQUE_VISIBLE_PRODUCT_REQUIRED', 'evidence': evidence}
            radio, title, product_id = candidates[0]
            await radio.check()
            if not await radio.is_checked():
                return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PRODUCT_RADIO_NOT_SELECTED'}
            await products.get_by_role('button', name='Siguiente', exact=True).click(timeout=10000)
            display = products.get_by_role('textbox', name='Nombre del producto', exact=True)
            await display.wait_for(state='visible', timeout=10000)
            label = await display.input_value()
            if not label.strip() or not title.startswith(label):
                return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PRODUCT_CONFIRMATION_NAME_MISMATCH'}
            confirmation = await self.evidence(page, payload, 'product-confirmation', read_only=True,
                selected_name=title, selected_id=product_id, display_name=label)
            confirm = products.get_by_role('button', name='Añadir', exact=True)
            if await confirm.count() != 1 or not await confirm.is_enabled():
                return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PRODUCT_CONFIRMATION_CONTROL_CHANGED', 'evidence': confirmation}
            receipt = self.save_product_receipt(payload, confirmation, title, product_id, label)
            submitted = True
            await confirm.click(timeout=10000)
            await display.wait_for(state='hidden', timeout=15000)
            attached = self.attached_product(page, label)
            await attached.wait_for(state='visible', timeout=10000)
            if await attached.count() != 1:
                return {'status': 'WAITING_MANUAL', 'performed': True, 'error_code': 'PRODUCT_ATTACHMENT_OUTCOME_UNKNOWN', 'evidence': {**confirmation, 'product_receipt': receipt}}
            evidence = await self.evidence(page, payload, 'product-attached', read_only=False, attached=True,
                confirmed=True, selected_name=title, selected_id=product_id, product_receipt=receipt,
                confirmation_screenshot=confirmation['screenshot'],
                dom_evidence={'selected_name': title, 'selected_id': product_id, 'attached': True,
                    'confirmation_text': await attached.inner_text(), 'url': page.url})
            proof = {'product_id': payload['product']['id'], 'account_id': payload['account']['id'],
                'environment_id': payload['account']['hubstudio_env_id'], 'video_sha256': payload['video']['sha256'],
                'mode': match['mode']}
            proof.update(selected_name=title, selected_id=product_id)
            return {'status': 'READY', 'performed': True, 'checkpoint': proof, 'evidence': evidence}
        except (BrowserTimeout, BrowserError):
            return {'status': 'WAITING_MANUAL', 'performed': submitted, 'error_code': 'PRODUCT_ATTACHMENT_OUTCOME_UNKNOWN' if submitted else 'PRODUCT_PICKER_NOT_READY',
                'evidence': await self.evidence(page, payload, 'product-pending', read_only=not submitted, product_receipt=receipt)}
        finally:
            if opened and not submitted:
                for dialog in (products, link):
                    if await dialog.is_visible():
                        await dialog.get_by_role('button', name='Cancelar', exact=True).click(timeout=5000)
                        await dialog.wait_for(state='hidden', timeout=5000)

    async def content_precheck(self, page, payload):
        if payload.get('checkpoint', {}).get('ATTACH_PRODUCT', {}).get('status') != 'READY':
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'VERIFIED_PRODUCT_ATTACHMENT_REQUIRED'}
        text = self.caption(payload)
        caption = page.locator('[data-e2e="caption_container"] [contenteditable="true"][role="combobox"]')
        attached_name = payload['checkpoint']['ATTACH_PRODUCT'].get('evidence', {}).get('dom_evidence', {}).get('confirmation_text')
        if text is None or await caption.count() != 1 or await caption.inner_text() != text:
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'CAPTION_CHANGED_BEFORE_CONTENT_REVIEW'}
        attached = self.attached_product(page, attached_name) if attached_name else None
        if attached is None or await attached.count() != 1 or not await attached.is_visible():
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PRODUCT_CHANGED_BEFORE_CONTENT_REVIEW'}
        heading = page.get_by_text('Revisión del contenido simplificada', exact=True)
        if await heading.count() != 1 or not await heading.is_visible():
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'CONTENT_REVIEW_CONTROL_CHANGED'}
        panel = heading.locator('..').locator('..')
        enabled = panel.locator('.headline-switch [aria-checked="true"]')
        if await enabled.count() != 1:
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'CONTENT_REVIEW_NOT_ENABLED'}
        status = panel.locator('.status-result[data-show="true"]')
        if await status.count() != 1 or not await status.is_visible():
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'CONTENT_REVIEW_STATE_UNKNOWN'}
        classes = (await status.get_attribute('class') or '').split()
        if 'status-checking' in classes:
            terminal = panel.locator('.status-result[data-show="true"]:not(.status-checking)')
            try:
                await terminal.wait_for(state='visible', timeout=self.config.content_review_timeout_seconds * 1000)
            except BrowserTimeout:
                evidence = await self.evidence(page, payload, 'content-review-checking', read_only=True, content_review='CHECKING')
                return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'CONTENT_REVIEW_TIMEOUT', 'evidence': evidence}
            status = panel.locator('.status-result[data-show="true"]')
            if await status.count() != 1 or not await status.is_visible():
                return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'CONTENT_REVIEW_STATE_UNKNOWN'}
            classes = (await status.get_attribute('class') or '').split()
        text = (await status.inner_text()).strip()
        passed = 'status-success' in classes and text.startswith('No se han detectado problemas.')
        state = 'PASSED' if passed else 'ERROR' if 'status-error' in classes else 'RESTRICTED' if 'status-warn' in classes else 'UNKNOWN'
        evidence = await self.evidence(page, payload, 'content-review-' + state.casefold(), read_only=True,
            content_review=state, caption_sha256=hashlib.sha256((self.caption(payload) or '').encode()).hexdigest(),
            selected_id=payload['checkpoint']['ATTACH_PRODUCT'].get('checkpoint', {}).get('selected_id'),
            dom_evidence={'panel': 'Revisión del contenido simplificada', 'enabled': True, 'visible': True,
                'data_show': await status.get_attribute('data-show'), 'classes': classes, 'text': text, 'url': page.url})
        return {'status': 'READY' if passed else 'WAITING_MANUAL', 'performed': False, 'evidence': evidence,
            **({} if passed else {'error_code': 'CONTENT_REVIEW_' + state})}

    @staticmethod
    async def identity(page):
        profile = page.get_by_role("link", name="Perfil", exact=True)
        opened = not await profile.is_visible()
        avatar = page.locator('button[data-tt="Header_NewHeader_Clickable"][aria-haspopup="dialog"]')
        if opened:
            if await avatar.count() != 1:
                return None
            await avatar.click()
            await profile.wait_for(state="visible", timeout=5000)
        href = await profile.get_attribute("href")
        try:
            base, parsed = urlsplit(page.url), urlsplit(urljoin(page.url, href or ""))
            match = re.fullmatch(r"/@([^/]+)", parsed.path)
            username = match[1] if (base.scheme == parsed.scheme == "https" and base.hostname == parsed.hostname == "www.tiktok.com"
                and not base.username and not base.password and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment and match) else None
        except ValueError:
            username = None
        if opened:
            await avatar.click()
        return username

    async def reconcile_upload(self, payload):
        intent = payload.get("checkpoint", {}).get("upload_intent", {})
        account, video = payload["account"], payload["video"]
        expected = {"account_id": account["id"], "environment_id": account.get("hubstudio_env_id"),
                    "cdp_endpoint": account["cdp_endpoint"], "video_sha256": video["sha256"],
                    "filename": Path(video["path"]).name}
        if not intent or any(intent.get(key) != value for key, value in expected.items()):
            return {"status": "WAITING_MANUAL", "performed": False, "error_code": "UPLOAD_INTENT_IDENTITY_CHANGED", "evidence": {"read_only": True}}
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(account["cdp_endpoint"], timeout=30000)
            try:
                page = self.upload_page(browser)
                if page is None or await self.identity(page) != account["expected_username"]:
                    return {"status": "WAITING_MANUAL", "performed": False, "error_code": "UPLOAD_ACCOUNT_CHANGED", "evidence": {"read_only": True}}
                filename = expected["filename"]
                block = page.locator('.info-main').filter(has=page.get_by_text(filename, exact=True))
                completion = block.locator('.info-status.success')
                complete = (await block.count() == 1 and await completion.count() == 1
                            and await completion.is_visible()
                            and await completion.locator('[data-testid="CheckCircleFill"]').count() == 1)
                text = (await completion.inner_text()).strip() if complete else ""
                complete = complete and bool(re.fullmatch(r"Cargado[（(]\d+(?:\.\d+)?(?:KB|MB|GB)[）)]", text))
                screenshot = self.screenshot_path(payload["publishing_task"]["id"], "upload-verified" if complete else "upload-reconciliation-pending")
                await page.screenshot(path=str(screenshot))
                body = await page.locator('body').inner_text()
                evidence = {"source": "OBSERVED_BROWSER", "read_only": True, "uploaded": complete,
                            "verified_at": datetime.now(timezone.utc).isoformat(), "screenshot": str(screenshot),
                            "publish_clicked": False, "content_review": "NOT_PASSED" if "Algo salió mal" in body else "NOT_CHECKED",
                            "dom_evidence": {"filename": filename, "upload_complete": complete, "completion_text": text, "url": page.url}}
                checkpoint = {key: expected[key] for key in ("video_sha256", "account_id", "environment_id", "filename")}
                return {"status": "READY" if complete else "WAITING_MANUAL", "checkpoint": checkpoint, "evidence": evidence,
                        **({} if complete else {"error_code": "UPLOAD_COMPLETION_NOT_YET_OBSERVED"})}
            finally:
                await browser.close()

    async def reconcile_product(self, payload):
        binding = attachment_binding(payload)
        intent = {key: binding[key] for key in ('product_id', 'account_id', 'environment_id', 'username',
            'video_sha256', 'country', 'mode', 'expected_names', 'requested_id')}
        if payload.get('checkpoint', {}).get('product_intent') != intent:
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PRODUCT_INTENT_IDENTITY_CHANGED', 'evidence': {'read_only': True}}
        account = payload['account']
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(account['cdp_endpoint'], timeout=30000)
            try:
                page = self.upload_page(browser)
                if page is None or await self.identity(page) != account['expected_username']:
                    return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PRODUCT_ACCOUNT_CHANGED', 'evidence': {'read_only': True}}
                caption = page.locator('[data-e2e="caption_container"] [contenteditable="true"][role="combobox"]')
                if not await self.uploaded(page, payload) or self.caption(payload) is None or await caption.count() != 1 or await caption.inner_text() != self.caption(payload):
                    return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'UPLOADED_VIDEO_AND_CAPTION_READBACK_REQUIRED', 'evidence': {'read_only': True}}
                labels = page.locator('[data-e2e="anchor_container"] .anchor-tag-label')
                if await labels.count() != 1 or not await labels.is_visible():
                    evidence = await self.evidence(page, payload, 'product-reconcile-pending', read_only=True)
                    return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'UNIQUE_ATTACHED_PRODUCT_REQUIRED', 'evidence': evidence}
                label = (await labels.inner_text()).strip()
                receipt = product_receipt(self.config, payload)
                if not receipt:
                    receipt = await self.save_product_readback(page, payload, label)
                if not receipt or receipt['receipt']['display_name'] != label:
                    evidence = await self.evidence(page, payload, 'product-reconcile-sku-unverified', read_only=True,
                        dom_evidence={'confirmation_text': label, 'url': page.url, 'attached': True})
                    return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PRODUCT_ATTACHMENT_SKU_UNVERIFIED', 'evidence': evidence}
                saved = receipt['receipt']
                evidence = await self.evidence(page, payload, 'product-reconciled', read_only=True, attached=True,
                    confirmed=True, selected_name=saved['selected_name'], selected_id=saved['selected_id'],
                    caption_sha256=binding['caption_sha256'], reconciliation='PLATFORM_ANCHOR_AND_CURRENT_CATALOG' if saved['source'] == 'OBSERVED_ATTACHED_PRODUCT_READBACK' else 'BOUND_RECEIPT_AND_CURRENT_CHIP',
                    product_receipt={'path': receipt['path'], 'sha256': receipt['sha256']},
                    dom_evidence={'selected_name': saved['selected_name'], 'selected_id': saved['selected_id'],
                        'attached': True, 'confirmation_text': label, 'url': page.url, 'caption': await caption.inner_text(), 'upload_complete': True})
                proof = {key: intent[key] for key in ('product_id', 'account_id', 'environment_id', 'video_sha256', 'mode')}
                proof.update(selected_name=saved['selected_name'], selected_id=saved['selected_id'])
                return {'status': 'READY', 'performed': False, 'checkpoint': proof, 'evidence': evidence}
            finally:
                await browser.close()

    async def observe_product(self, payload):
        account, product = payload['account'], payload['product']
        match = payload.get('product_match', {})
        selection_mode = match.get('mode', payload.get('checkpoint', {}).get('product_selection_mode', self.config.product_selection_mode))
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(account['cdp_endpoint'], timeout=30000)
            page = self.upload_page(browser)
            opened_link = opened_products = False
            try:
                if page is None or await self.identity(page) != account['expected_username']:
                    return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PRODUCT_ACCOUNT_CHANGED', 'evidence': {'read_only': True}}
                products = page.get_by_role('dialog', name='Añade enlaces de productos', exact=True)
                link = page.get_by_role('dialog', name='Añade un enlace', exact=True)
                if not await products.is_visible():
                    if not await link.is_visible():
                        await page.get_by_role('button', name='Añadir', exact=True).click(timeout=10000)
                        opened_link = True
                    if (await link.get_by_role('combobox').inner_text()).strip() != 'Productos':
                        return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PRODUCT_LINK_TYPE_NOT_SELECTED', 'evidence': {'read_only': True}}
                    await link.get_by_role('button', name='Siguiente', exact=True).click(timeout=10000)
                    opened_products = True
                await products.get_by_role('radio').first.wait_for(state='visible', timeout=10000)
                headers = await products.locator('tr').first.locator('th').all_text_contents()
                if [text.strip() for text in headers] != ['Nombre del producto', 'ID del producto', 'Precio', 'Existencias', '', 'Estado']:
                    return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PRODUCT_TABLE_SCHEMA_CHANGED', 'evidence': {'read_only': True}}
                candidates = []
                expected_names = [' '.join(name.casefold().split()) for name in match.get('expected_names', [product['name']]) if name.strip()]
                requested_id = (match.get('requested_id', product.get('platform_product_id')) or '').strip()
                for row in await products.locator('tr').all():
                    radio = row.get_by_role('radio')
                    if await radio.count() != 1 or not await row.is_visible() or not await radio.is_visible():
                        continue
                    cells = await row.locator('td').all_text_contents()
                    title = (await radio.get_attribute('value') or '').strip()
                    if len(cells) != 6 or not title or not cells[1].strip():
                        return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PRODUCT_ROW_INCOMPLETE', 'evidence': {'read_only': True}}
                    normalized = ' '.join(title.casefold().split())
                    name_match = any(normalized == expected or normalized.startswith(expected + ' ') for expected in expected_names)
                    product_id = cells[1].strip()
                    matches = product_id == requested_id if selection_mode == 'exact_id' else name_match
                    availability = cells[-1].strip()
                    selectable = availability == 'Activo' and await radio.is_enabled()
                    candidates.append({'product_name': title, 'product_id': product_id, 'availability': availability,
                                       'status': availability, 'selectable': selectable, 'matches': matches and selectable})
                screenshot = self.screenshot_path(payload['publishing_task']['id'], 'product-candidates')
                await page.screenshot(path=str(screenshot))
                return {'status': 'READY', 'evidence': {'source': 'OBSERVED_BROWSER', 'read_only': True,
                    'account_id': account['id'], 'environment_id': account['hubstudio_env_id'], 'username': account['expected_username'],
                    'video_sha256': payload['video']['sha256'], 'verified_at': datetime.now(timezone.utc).isoformat(),
                    'url': page.url, 'screenshot': str(screenshot), 'candidates': candidates,
                    'match_count': sum(row['matches'] for row in candidates), 'scope': 'VISIBLE_CATALOG_PAGE_ONLY',
                    'attached': False, 'confirmed': False, 'publish_clicked': False}}
            except BrowserTimeout:
                return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PRODUCT_PICKER_NOT_READY', 'evidence': {'read_only': True}}
            finally:
                if page is not None:
                    products = page.get_by_role('dialog', name='Añade enlaces de productos', exact=True)
                    if opened_products and await products.is_visible():
                        await products.get_by_role('button', name='Cancelar', exact=True).click(timeout=5000)
                    link = page.get_by_role('dialog', name='Añade un enlace', exact=True)
                    if opened_link and await link.is_visible():
                        await link.get_by_role('button', name='Cancelar', exact=True).click(timeout=5000)
                await browser.close()

    async def network(self, context, account):
        if account.get("hubstudio_env_id") not in self.config.network_probe_environment_ids:
            return None
        tab = await context.new_page()
        try:
            await tab.goto("https://ipinfo.io/json", wait_until="domcontentloaded", timeout=30000)
            result = json.loads(await tab.locator("body").inner_text())
            aliases = {"MX": {"墨西哥", "Mexico", "México", "MX"}}
            code = result.get("country")
            if account["country"] not in aliases.get(code, {code}):
                return {"observed_country_code": code, "country": code, "source": "OBSERVED_NETWORK",
                        "verified_at": datetime.now(timezone.utc).isoformat(), "provider": "ipinfo.io"}
            return {"country": account["country"], "observed_country_code": code, "source": "OBSERVED_NETWORK",
                    "verified_at": datetime.now(timezone.utc).isoformat(), "provider": "ipinfo.io"}
        finally:
            await tab.close()

    async def preflight(self, payload):
        account = payload["account"]
        endpoint = account.get("cdp_endpoint")
        if not endpoint or not self.config.manual_cdp_allowed:
            return {"status": "WAITING_MANUAL", "checks": [{"name": "existing_browser", "status": "WAITING_MANUAL", "reason": "OPEN_EXISTING_HUB_ENVIRONMENT"}], "evidence": {}}
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(endpoint, timeout=30000)
            try:
                page = self.upload_page(browser)
                if page is None:
                    return {"status": "WAITING_MANUAL", "checks": [{"name": "upload_page", "status": "WAITING_MANUAL", "reason": "ONE_EXISTING_TIKTOK_UPLOAD_PAGE_REQUIRED"}], "evidence": {}}
                username = await self.identity(page)
                identity = {"source": "OBSERVED_BROWSER", "username": username, "environment_id": account.get("hubstudio_env_id"),
                            "verified_at": datetime.now(timezone.utc).isoformat(), "url": page.url}
                vpn = await self.network(page.context, account)
                body = await page.locator("body").inner_text()
                existing_draft = "no se ha guardado" in body or "seguir editándolo" in body
                checkpoint = payload.get("checkpoint", {}).get("UPLOAD_VIDEO", {})
                intent = payload.get("checkpoint", {}).get("upload_intent", {})
                reconcile_existing = bool(intent) and all(intent.get(key) == value for key, value in {
                    "account_id": account["id"], "environment_id": account.get("hubstudio_env_id"),
                    "cdp_endpoint": endpoint, "video_sha256": payload["video"]["sha256"],
                    "filename": Path(payload["video"]["path"]).name}.items())
                input_ready = await page.locator('input[type="file"][accept="video/*"]').count() == 1
                checks = [{"name": "hubstudio_environment", "status": "READY", "reason": "USER_OPENED_ENVIRONMENT; API_DISPATCH_NOT_CONNECTED"},
                          {"name": "tiktok_identity", "status": "READY" if username == account.get("expected_username") else "WAITING_MANUAL", "reason": "CURRENT_PROFILE_LINK_OBSERVED"},
                          {"name": "vpn", "status": "READY" if vpn and vpn.get("country") == account["country"] else "WAITING_MANUAL", "reason": "OBSERVED_NETWORK_COUNTRY" if vpn else "NETWORK_PROBE_NOT_AUTHORIZED"},
                          {"name": "upload_page", "status": "READY" if (input_ready and not existing_draft) or checkpoint.get("status") == "READY" or reconcile_existing else "WAITING_MANUAL", "reason": "READ_ONLY_EXISTING_UPLOAD_RECONCILIATION" if reconcile_existing else "EXISTING_DRAFT_DO_NOT_DISCARD" if existing_draft else "ACTUAL_FILE_INPUT_OBSERVED"}]
                return {"status": "READY" if all(row["status"] == "READY" for row in checks) else "WAITING_MANUAL", "checks": checks,
                        "evidence": {"identity": identity, "vpn": vpn or {}, "environment_dispatch": "WAITING_MANUAL",
                            "public_submission": "MANUAL_CONFIRMATION_REQUIRED" if self.config.allow_publish and not self.config.stop_after_upload
                                and payload.get("checkpoint", {}).get("execution_scope") != "UPLOAD_ONLY" else "DISABLED"}}
            finally:
                await browser.close()

    @staticmethod
    def values(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from ExistingHubBrowserAdapter.values(item)
        elif isinstance(value, list):
            for item in value:
                yield from ExistingHubBrowserAdapter.values(item)
        elif isinstance(value, (str, int)):
            yield str(value)

    @staticmethod
    def publication_candidate(request_body, response_body, intent, caption):
        try:
            submitted = json.loads(request_body)
        except (TypeError, ValueError):
            return None
        values = set(ExistingHubBrowserAdapter.values(submitted))
        if caption not in values or intent['selected_product_id'] not in values:
            return None
        urls = []
        for value in ExistingHubBrowserAdapter.values(response_body):
            if not value.startswith('https://'):
                continue
            parsed = urlsplit(value)
            match = re.fullmatch(r'/@([^/]+)/video/(\d+)', parsed.path)
            if (parsed.scheme == 'https' and parsed.hostname == 'www.tiktok.com'
                    and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                    and match and match[1] == intent['username']):
                urls.append((match[2], value))
        unique = set(urls)
        if len(unique) != 1:
            return None
        identifier, url = unique.pop()
        return {'video_id': identifier, 'video_url': url,
                'source': 'BOUND_PLATFORM_POST_RESPONSE',
                'response_sha256': hashlib.sha256(json.dumps(response_body, sort_keys=True).encode()).hexdigest()}

    async def submit_publication(self, payload):
        row, intent = payload['publishing_task'], payload.get('checkpoint', {}).get('submission_intent', {})
        confirmation = payload.get('checkpoint', {}).get('single_task_confirmation', {})
        if (not self.config.allow_publish or self.config.stop_after_upload
                or payload.get('checkpoint', {}).get('execution_scope') == 'UPLOAD_ONLY'):
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PUBLIC_SUBMISSION_DISABLED'}
        if (not intent or intent.get('publishing_task_id') != row['id'] or row.get('submission_state') != 'SUBMITTING'
                or confirmation.get('confirmed') is not True or confirmation.get('fingerprint') != intent.get('fingerprint')):
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PERSISTED_SUBMISSION_INTENT_REQUIRED'}
        if not self.submission_identity_matches(payload, intent):
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'SUBMISSION_INPUT_IDENTITY_CHANGED'}
        clicked = False
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(payload['account']['cdp_endpoint'], timeout=30000)
            captures = set()
            receipt = asyncio.get_running_loop().create_future()
            try:
                page = self.upload_page(browser)
                if page is None or await self.identity(page) != intent['username'] or not await self.uploaded(page, payload):
                    return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'UPLOAD_ACCOUNT_OR_FILE_CHANGED'}
                checked = await self.content_precheck(page, payload)
                if checked['status'] != 'READY':
                    return checked
                control = page.get_by_role('button', name='Publicar', exact=True)
                if await control.count() != 1 or not await control.is_visible() or not await control.is_enabled():
                    return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PUBLISH_CONTROL_NOT_OBSERVED'}
                before_url = page.url
                evidence = await self.evidence(page, payload, 'before-public-submit', read_only=True)
                async def observe_response(response):
                    if response.request.method != 'POST' or urlsplit(response.url).hostname not in {'www.tiktok.com', 'tiktok.com'}:
                        return
                    try:
                        result = self.publication_candidate(response.request.post_data, await response.json(), intent, row['caption'])
                    except (ValueError, BrowserError):
                        return
                    if result and not receipt.done():
                        receipt.set_result({**result, 'response_url': response.url})
                def capture(response):
                    task = asyncio.create_task(observe_response(response))
                    captures.add(task)
                    task.add_done_callback(captures.discard)
                page.on('response', capture)
                clicked = True
                await control.click(timeout=15000)
                try:
                    candidate = await asyncio.wait_for(receipt, self.config.publication_receipt_timeout_seconds)
                except asyncio.TimeoutError:
                    candidate = None
                evidence.update(publishing_task_id=row['id'], submission_intent_id=intent['id'],
                    description_sha256=hashlib.sha256(row['caption'].encode()).hexdigest(),
                    selected_id=intent['selected_product_id'], read_only=False, publish_clicked=True,
                    dom_evidence={'before_url': before_url, 'publish_control_observed': True, 'enabled': True, 'visible': True,
                        'caption': row['caption'], 'selected_id': intent['selected_product_id']})
                evidence['screenshot'] = str(self.screenshot_path(row['id'], 'public-submit-result'))
                await page.screenshot(path=evidence['screenshot'])
                return {'status': 'READY', 'performed': True, 'evidence': evidence,
                    'checkpoint': {'publication_candidate': candidate}}
            except BrowserTimeout:
                return {'status': 'WAITING_MANUAL', 'performed': None if clicked else False,
                    'error_code': 'SUBMISSION_OUTCOME_UNKNOWN' if clicked else 'PUBLISH_PAGE_NOT_READY'}
            finally:
                pending = tuple(captures)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await browser.close()

    @staticmethod
    def submission_identity_matches(payload, intent):
        return all(value and intent.get(key) == value for key, value in submission_binding(payload).items())

    async def studio_candidate(self, page, payload):
        if await self.identity(page) != payload['account']['expected_username']:
            return None
        observed = await page.locator('a[data-tt="components_PostInfoCell_a"][href]').evaluate_all("""nodes => nodes.map(anchor => {
            const row = anchor.closest('[data-tt="components_RowLayout_FlexRow"]');
            if (!row) return null;
            const key = Object.keys(row).find(key => key.startsWith('__reactFiber$'));
            let fiber = key && row[key];
            for (let i = 0; i < 3; i++) fiber = fiber?.return;
            const values = [fiber?.memoizedProps?.item, fiber?.alternate?.memoizedProps?.item]
                .filter(item => item && typeof item === 'object').map(item => ({item_id: item.item_id,
                    create_time: item.create_time, post_time: item.post_time, desc: item.desc, duration: item.duration,
                    in_review: item.in_review, status: item.status, visibility: item.visibility}));
            if (!values.length || values.some(item => JSON.stringify(item) !== JSON.stringify(values[0]))) return null;
            return {...values[0], href: anchor.href, row_text: row.innerText};
        }).filter(Boolean)""")
        matches = [item for item in observed if studio_item_matches(item, payload)]
        if len(matches) != 1:
            return None
        item = matches[0]
        evidence = await self.evidence(page, payload, 'publication-studio-readback', read_only=True)
        screenshot = Path(evidence['screenshot'])
        receipt = {'source': 'OBSERVED_STUDIO_POST_READBACK', 'created_at': evidence['verified_at'],
            'binding': submission_binding(payload), 'submission_intent_sha256': hashlib.sha256(json.dumps(
                payload['checkpoint']['submission_intent'], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'step_sha256': {name: hashlib.sha256(json.dumps(payload['checkpoint'][name], ensure_ascii=False,
                sort_keys=True, separators=(',', ':')).encode()).hexdigest() for name in ('UPLOAD_VIDEO', 'FILL_DESCRIPTION', 'ATTACH_PRODUCT', 'SUBMIT')},
            'item': item, 'screenshot': str(screenshot), 'screenshot_sha256': hashlib.sha256(screenshot.read_bytes()).hexdigest()}
        path = screenshot.with_suffix('.json')
        raw = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        with path.open('xb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        return {'source': receipt['source'], 'video_id': item['item_id'], 'video_url': item['href'], 'create_time': item['create_time'],
            'public_visible': item.get('in_review') is False and item.get('status') == 102 and item.get('visibility') == 1
                and 'Todo el mundo' in item.get('row_text', ''),
            'studio_receipt': {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}}

    @staticmethod
    def product_link_matches(link, product_id):
        try:
            parsed = urlsplit(link or '')
        except ValueError:
            return False
        if parsed.scheme != 'https' or parsed.hostname != 'www.tiktok.com' or parsed.username or parsed.password:
            return False
        identifiers = {*parsed.path.split('/'), *(value for values in parse_qs(parsed.query).values() for value in values)}
        return product_id in identifiers

    @staticmethod
    async def public_post_metadata(page):
        script = page.locator('script#__UNIVERSAL_DATA_FOR_REHYDRATION__')
        if await script.count() != 1:
            return None
        return await script.evaluate("""node => {
            let detail;
            try { detail = JSON.parse(node.textContent)?.__DEFAULT_SCOPE__?.['webapp.video-detail']; }
            catch { return null; }
            const item = detail?.itemInfo?.itemStruct;
            if (!item || detail.statusCode !== 0) return null;
            const products = [];
            for (const anchor of item.anchors || []) {
                if (anchor.type !== 35 || typeof anchor.extra !== 'string') continue;
                let entries;
                try { entries = JSON.parse(anchor.extra); } catch { return null; }
                if (!Array.isArray(entries)) return null;
                for (const entry of entries) if (entry.type === 33)
                    products.push({id: entry.id, keyword: entry.keyword});
            }
            return {id: item.id, username: item.author?.uniqueId, desc: item.desc, create_time: item.createTime,
                private_item: item.privateItem, restricted_visibility: item.secret, friends_only: item.forFriend,
                prohibited: item.isProhibited, taken_down: item.takeDown, reviewing: item.isReviewing,
                ecommerce_video: item.isECVideo, duration: item.video?.duration, products};
        }""")

    @staticmethod
    def public_post_matches(metadata, candidate, payload):
        if not metadata:
            return False
        intent = payload['checkpoint']['submission_intent']
        try:
            started = datetime.fromisoformat(intent['started_at'].replace('Z', '+00:00')).timestamp()
            return (metadata.get('id') == candidate['video_id'] and metadata.get('username') == intent['username']
                and metadata.get('desc') == payload['publishing_task']['caption']
                and started <= int(metadata['create_time']) <= datetime.now(timezone.utc).timestamp()
                and (not candidate.get('create_time') or int(metadata['create_time']) == int(candidate['create_time']))
                and all(metadata.get(key) is False for key in ('private_item', 'restricted_visibility', 'friends_only', 'prohibited', 'reviewing'))
                and metadata.get('taken_down') == 0 and metadata.get('ecommerce_video') == 1
                and abs(float(metadata['duration']) - float(payload['video']['duration'])) <= 1
                and [item.get('id') for item in metadata.get('products', [])] == [intent['selected_product_id']])
        except (ValueError, TypeError, KeyError):
            return False

    @staticmethod
    async def public_playback(page, duration):
        await page.wait_for_function("""duration => [...document.querySelectorAll('video')].some(video => {
            const box = video.getBoundingClientRect();
            return box.width > 0 && box.height > 0 && video.readyState >= 2 && !video.error
                && video.videoWidth > 0 && video.videoHeight > 0 && Math.abs(video.duration - duration) <= 1;
        })""", arg=duration, timeout=30000)
        videos = await page.locator('video').evaluate_all("""nodes => nodes.map(video => {
            const box = video.getBoundingClientRect();
            return {duration: video.duration, ready_state: video.readyState, error: video.error?.code || null,
                visible: box.width > 0 && box.height > 0, width: video.videoWidth, height: video.videoHeight};
        })""")
        matches = [video for video in videos if video.get('visible') is True and video.get('ready_state', 0) >= 2
            and video.get('error') is None and video.get('width', 0) > 0 and video.get('height', 0) > 0
            and isinstance(video.get('duration'), (float, int)) and abs(video['duration'] - duration) <= 1]
        return matches[0] if len(matches) == 1 else None

    async def verify_publication(self, payload):
        row, intent = payload['publishing_task'], payload.get('checkpoint', {}).get('submission_intent', {})
        candidate = payload.get('checkpoint', {}).get('SUBMIT', {}).get('checkpoint', {}).get('publication_candidate')
        if not intent:
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PUBLICATION_RECEIPT_REQUIRED',
                    'evidence': {'read_only': True}}
        if not self.submission_identity_matches(payload, intent):
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'SUBMISSION_INPUT_IDENTITY_CHANGED'}
        if not candidate and (row.get('submission_state') not in {'SUBMITTED', 'UNKNOWN'} or not payload['video'].get('duration')
                or not all(payload.get('checkpoint', {}).get(name) for name in ('UPLOAD_VIDEO', 'FILL_DESCRIPTION', 'ATTACH_PRODUCT', 'SUBMIT'))):
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PUBLICATION_RECEIPT_REQUIRED', 'evidence': {'read_only': True}}
        parsed = urlsplit(candidate.get('video_url', '')) if candidate else None
        if candidate and (candidate.get('source') != 'BOUND_PLATFORM_POST_RESPONSE' or parsed.scheme != 'https' or parsed.hostname != 'www.tiktok.com' or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path != f"/@{intent['username']}/video/{candidate['video_id']}"):
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PUBLICATION_RECEIPT_IDENTITY_CHANGED'}
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(payload['account']['cdp_endpoint'], timeout=30000)
            page = None
            try:
                contexts = [context for context in browser.contexts if any(urlsplit(tab.url).hostname == 'www.tiktok.com'
                    for tab in context.pages)]
                if len(contexts) != 1:
                    return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PUBLICATION_ACCOUNT_CHANGED'}
                if not candidate:
                    studio = [tab for tab in contexts[0].pages if urlsplit(tab.url).hostname == 'www.tiktok.com'
                        and urlsplit(tab.url).path == '/tiktokstudio/content']
                    if len(studio) != 1:
                        return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PUBLICATION_STUDIO_READBACK_REQUIRED', 'evidence': {'read_only': True}}
                    candidate = await self.studio_candidate(studio[0], payload)
                    if not candidate or not studio_readback(self.config, payload, candidate['studio_receipt']):
                        return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PUBLICATION_RECORD_NOT_UNIQUELY_BOUND', 'evidence': {'read_only': True}}
                    if not candidate['public_visible']:
                        return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PUBLICATION_PENDING_PLATFORM_REVIEW',
                            'checkpoint': {'publication_observation': candidate},
                            'evidence': await self.evidence(studio[0], payload, 'publication-not-public', read_only=True,
                                published=False, studio_receipt=candidate['studio_receipt'])}
                page = await contexts[0].new_page()
                await page.goto(candidate['video_url'], wait_until='domcontentloaded', timeout=30000)
                try:
                    await page.get_by_role('link', name='Perfil', exact=True).wait_for(state='visible', timeout=30000)
                except (BrowserTimeout, BrowserError):
                    return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PUBLICATION_ACCOUNT_NAV_NOT_READY',
                        'checkpoint': {'publication_observation': candidate},
                        'evidence': await self.evidence(page, payload, 'publication-account-nav-pending', read_only=True,
                            authenticated_navigation_observed=False,
                            required_navigation={'role': 'link', 'name': 'Perfil', 'state': 'visible'},
                            **({'studio_receipt': candidate['studio_receipt']} if candidate.get('studio_receipt') else {}))}
                if await self.identity(page) != intent['username']:
                    return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PUBLICATION_ACCOUNT_CHANGED'}
                playback = await self.public_playback(page, payload['video']['duration']) if candidate.get('studio_receipt') else None
                if candidate.get('studio_receipt') and not playback:
                    return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PUBLICATION_PLAYBACK_NOT_UNIQUE',
                        'checkpoint': {'publication_observation': candidate}, 'evidence': {'read_only': True}}
                if not candidate.get('studio_receipt'):
                    await page.locator('video').wait_for(state='visible', timeout=30000)
                body = await page.locator('body').inner_text()
                canonical = page.locator('link[rel="canonical"]')
                href = await canonical.get_attribute('href') if await canonical.count() == 1 else page.url
                product_links = await page.locator('a[href]').evaluate_all("(nodes) => nodes.map(n => n.getAttribute('href'))")
                public = await self.public_post_metadata(page) if candidate.get('studio_receipt') else None
                product_verified = self.public_post_matches(public, candidate, payload) if candidate.get('studio_receipt') else any(
                    self.product_link_matches(link, intent['selected_product_id']) for link in product_links)
                if (href != candidate['video_url'] or row['caption'] not in body or not product_verified):
                    return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PUBLICATION_PAGE_IDENTITY_NOT_VERIFIED',
                        'checkpoint': {'publication_observation': candidate},
                        'evidence': await self.evidence(page, payload, 'publication-pending', read_only=True,
                            **({'studio_receipt': candidate['studio_receipt']} if candidate.get('studio_receipt') else {}))}
                evidence = await self.evidence(page, payload, 'publication-verified', read_only=True, published=True,
                    publishing_task_id=row['id'], submission_intent_id=intent['id'],
                    description_sha256=hashlib.sha256(row['caption'].encode()).hexdigest(),
                    **({'studio_receipt': candidate['studio_receipt']} if candidate.get('studio_receipt') else {}),
                    **({'public_metadata': public} if public is not None else {}),
                    **({'playback': playback} if playback is not None else {}),
                    dom_evidence={'video_url': href, 'video_id': candidate['video_id'], 'published': True,
                        'username': intent['username'], 'caption': row['caption'], 'selected_product_id': intent['selected_product_id']})
                return {'status': 'READY', 'performed': False, 'platform_video_id': candidate['video_id'],
                    'platform_url': href, 'evidence': evidence}
            except BrowserTimeout:
                return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'PUBLICATION_VERIFICATION_TIMEOUT',
                        'evidence': {'read_only': True}}
            finally:
                if page is not None:
                    await page.close()
                await browser.close()

    async def execute_step(self, name, payload):
        if name == 'SUBMIT':
            return await self.submit_publication(payload)
        if name == 'VERIFY_RESULT':
            return await self.verify_publication(payload)
        if name in {'FILL_DESCRIPTION', 'ATTACH_PRODUCT', 'CONTENT_PRECHECK'}:
            if payload.get('checkpoint', {}).get('UPLOAD_VIDEO', {}).get('status') != 'READY':
                return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'VERIFIED_UPLOADED_VIDEO_REQUIRED'}
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(payload['account']['cdp_endpoint'], timeout=30000)
                try:
                    page = self.upload_page(browser)
                    if page is None or await self.identity(page) != payload['account']['expected_username']:
                        return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'UPLOAD_ACCOUNT_CHANGED'}
                    if not await self.uploaded(page, payload):
                        return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'UPLOADED_FILE_CHANGED'}
                    if name == 'FILL_DESCRIPTION':
                        return await self.fill_description(page, payload)
                    if name == 'ATTACH_PRODUCT':
                        return await self.attach_product(page, payload)
                    return await self.content_precheck(page, payload)
                finally:
                    await browser.close()
        if name != "UPLOAD_VIDEO":
            return {"status": "WAITING_MANUAL", "performed": False, "error_code": "OBSERVED_PRODUCT_CONTENT_REVIEW_REQUIRED", "evidence": {"step": name}}
        video = payload["video"]
        path = Path(video["path"])
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != video["sha256"]:
            return {"status": "FAILED", "performed": False, "error_code": "UPLOAD_INPUT_HASH_CHANGED", "evidence": {}}
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(payload["account"]["cdp_endpoint"], timeout=30000)
            try:
                page = self.upload_page(browser)
                if page is None or await self.identity(page) != payload["account"]["expected_username"]:
                    return {"status": "WAITING_MANUAL", "performed": False, "error_code": "UPLOAD_ACCOUNT_CHANGED", "evidence": {}}
                body = await page.locator("body").inner_text()
                if "no se ha guardado" in body or "seguir editándolo" in body:
                    return {"status": "WAITING_MANUAL", "performed": False, "error_code": "EXISTING_DRAFT_DO_NOT_DISCARD", "evidence": {"url": page.url}}
                control = page.locator('input[type="file"][accept="video/*"]')
                if await control.count() != 1:
                    return {"status": "WAITING_MANUAL", "performed": False, "error_code": "UPLOAD_INPUT_NOT_FOUND", "evidence": {"url": page.url}}
                if await page.locator('.info-main').count():
                    return {"status": "WAITING_MANUAL", "performed": False, "error_code": "EXISTING_UPLOAD_DO_NOT_REPLACE",
                            "evidence": await self.evidence(page, payload, 'existing-upload', read_only=True)}
                await page.screenshot(path=str(self.screenshot_path(payload["publishing_task"]["id"], "before-upload")))
                await control.set_input_files(str(path))
                screenshot = self.screenshot_path(payload["publishing_task"]["id"], "upload-submitted")
                await page.screenshot(path=str(screenshot))
                block = page.locator('.info-main').filter(has=page.get_by_text(path.name, exact=True))
                await block.locator('.info-status.success').wait_for(state='visible', timeout=self.config.upload_timeout_seconds * 1000)
                if not await self.uploaded(page, payload):
                    return {"status": "WAITING_MANUAL", "error_code": "UPLOAD_COMPLETION_NOT_YET_OBSERVED",
                            "evidence": {"upload_submitted_once": True, "url": page.url, "screenshot": str(screenshot)}}
                evidence = await self.evidence(page, payload, 'upload-completed', uploaded=True, read_only=False,
                    upload_submitted_once=True, dom_evidence={'filename': path.name, 'upload_complete': True,
                        'completion_text': (await block.locator('.info-status.success').inner_text()).strip(), 'url': page.url})
                proof = {'video_sha256': video['sha256'], 'account_id': payload['account']['id'],
                    'environment_id': payload['account']['hubstudio_env_id'], 'filename': path.name}
                return {'status': 'READY', 'performed': True, 'checkpoint': proof, 'evidence': evidence}
            except BrowserTimeout:
                return {"status": "WAITING_MANUAL", "error_code": "UPLOAD_OUTCOME_UNKNOWN", "evidence": {"url": page.url, "publish_clicked": False}}
            finally:
                await browser.close()
