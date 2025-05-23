import asyncio
from collections import Counter
import hashlib
import platform
import sys
import time

import falcon
import falcon.asgi
import falcon.errors
import falcon.util

SSE_TEST_MAX_DELAY_SEC = 1
_WIN32 = sys.platform.startswith('win')
_X86_64 = platform.machine() == 'x86_64'


class Things:
    def __init__(self):
        self._counter = Counter()

    async def on_get(self, req, resp):
        await asyncio.sleep(0.01)
        resp.text = req.remote_addr

    async def on_post(self, req, resp):
        resp.data = await req.stream.read(req.content_length or 0)
        resp.set_header('X-Counter', str(self._counter['background:things:on_post']))

        async def background_job_async():
            await asyncio.sleep(0.01)
            self._counter['background:things:on_post'] += 1

        def background_job_sync():
            time.sleep(0.01)
            self._counter['background:things:on_post'] += 1000

        resp.schedule(background_job_async)
        resp.schedule_sync(background_job_sync)
        resp.schedule(background_job_async)
        resp.schedule_sync(background_job_sync)

    async def on_put(self, req, resp):
        # NOTE(kgriffs): Test that reading past the end does
        # not hang.

        chunks = []
        for i in range(req.content_length + 1):
            # NOTE(kgriffs): In the ASGI interface, bounded_stream is an
            #   alias for req.stream. We'll use the alias here just as
            #   a sanity check.
            chunk = await req.bounded_stream.read(1)
            chunks.append(chunk)

        # NOTE(kgriffs): body should really be set to a string, but
        #   Falcon is lenient and will allow bytes as well (although
        #   it is slightly less performant).
        # TODO(kgriffs): Perhaps in Falcon 4.0 be more strict? We would
        #   also have to change the WSGI behavior to match.
        resp.text = b''.join(chunks)

        # =================================================================
        # NOTE(kgriffs): Test the sync_to_async helpers here to make sure
        #   they work as expected in the context of a real ASGI server.
        # =================================================================
        safely_tasks = []
        safely_values = []

        def callmesafely(a, b, c=None):
            # NOTE(kgriffs): Sleep to prove that there isn't another instance
            #   running in parallel that is able to race ahead.
            time.sleep(0.001)
            safely_values.append((a, b, c))

        cms = falcon.util.wrap_sync_to_async(callmesafely, threadsafe=False)

        # NOTE(caselit): on windows it takes more time so create less tasks
        # NOTE(vytas): Tests on non-x86 platforms are run using software
        #   emulation via single-thread QEMU Docker containers, making them
        #   considerably slower as well.
        num_cms_tasks = 100 if _WIN32 or not _X86_64 else 1000

        for i in range(num_cms_tasks):
            # NOTE(kgriffs): create_task() is used here, so that the coroutines
            #   are scheduled immediately in the order created; under Python
            #   3.6, asyncio.gather() does not seem to always schedule
            #   them in order, so we do it this way to make it predictable.
            safely_tasks.append(asyncio.create_task(cms(i, i + 1, c=i + 2)))

        await asyncio.gather(*safely_tasks)

        assert len(safely_values) == num_cms_tasks
        for i, val in enumerate(safely_values):
            assert safely_values[i] == (i, i + 1, i + 2)

        def callmeshirley(a=42, b=None):
            return (a, b)

        assert (42, None) == await falcon.util.sync_to_async(callmeshirley)
        assert (1, 2) == await falcon.util.sync_to_async(callmeshirley, 1, 2)
        assert (5, None) == await falcon.util.sync_to_async(callmeshirley, 5)
        assert (3, 4) == await falcon.util.sync_to_async(callmeshirley, 3, b=4)


class Bucket:
    async def on_post(self, req, resp):
        resp.text = await req.stream.read()

    async def on_put_drops(self, req, resp):
        # NOTE(kgriffs): Integrity check
        sha1 = hashlib.sha1()

        drops = 0
        async for drop in req.stream:
            drops += 1
            sha1.update(drop)

        resp.media = {'drops': drops, 'sha1': sha1.hexdigest()}


class Feed:
    async def on_websocket(self, req, ws, feed_id):
        await ws.accept()
        await ws.send_text(feed_id)


class Events:
    async def on_get(self, req, resp):
        async def emit():
            s = 0
            while s <= SSE_TEST_MAX_DELAY_SEC:
                yield falcon.asgi.SSEvent(text='hello world')
                await asyncio.sleep(s)
                s += SSE_TEST_MAX_DELAY_SEC / 4

        resp.sse = emit()

    async def on_websocket(self, req, ws):  # noqa: C901
        recv_command = req.get_header('X-Command') == 'recv'
        send_mismatched = req.get_header('X-Mismatch') == 'send'
        recv_mismatched = req.get_header('X-Mismatch') == 'recv'

        mismatch_type = req.get_header('X-Mismatch-Type', default='text')

        raise_error = req.get_header('X-Raise-Error')

        close = req.get_header('X-Close')
        close_code = req.get_header('X-Close-Code')
        if close_code:
            close_code = int(close_code)

        accept = req.get_header('X-Accept', default='accept')

        if accept == 'accept':
            subprotocol = req.get_header('X-Subprotocol')

            if subprotocol == '*':
                subprotocol = ws.subprotocols[0]

            if subprotocol:
                await ws.accept(subprotocol)
            else:
                await ws.accept()
        elif accept == 'reject':
            if close:
                await ws.close()
            return

        if send_mismatched:
            if mismatch_type == 'text':
                await ws.send_text(b'fizzbuzz')
            else:
                await ws.send_data('fizzbuzz')

        if recv_mismatched:
            if mismatch_type == 'text':
                await ws.receive_text()
            else:
                await ws.receive_data()

        start = time.time()
        while time.time() - start < 1:
            try:
                msg = None

                if recv_command:
                    msg = await ws.receive_media()
                else:
                    msg = None

                await ws.send_text('hello world')
                print('on_websocket:send_text')

                if msg and msg['command'] == 'echo':
                    await ws.send_text(msg['echo'])

                await ws.send_data(b'hello\x00world')
                await asyncio.sleep(0.2)
            except falcon.errors.WebSocketDisconnected:
                print('on_websocket:WebSocketDisconnected')
                raise

            if raise_error == 'generic':
                raise Exception('Test: Generic Unhandled Error')
            elif raise_error == 'http':
                raise falcon.HTTPBadRequest()

        if close:
            # NOTE(kgriffs): Tests that the default is used
            #   when close_code is None.
            await ws.close(close_code)


class Multipart:
    async def on_post(self, req, resp):
        parts = {}

        form = await req.get_media()
        async for part in form:
            # NOTE(vytas): SHA1 is no longer recommended for cryptographic
            #   purposes, but here we are only using it for integrity checking.
            sha1 = hashlib.sha1()
            async for chunk in part.stream:
                sha1.update(chunk)

            parts[part.name] = {
                'filename': part.filename,
                'sha1': sha1.hexdigest(),
            }

        resp.media = parts


class LifespanHandler:
    def __init__(self):
        self.startup_succeeded = False
        self.shutdown_succeeded = False

    async def process_startup(self, scope, event):
        assert scope['type'] == 'lifespan'
        assert event['type'] == 'lifespan.startup'
        self.startup_succeeded = True

    async def process_shutdown(self, scope, event):
        assert scope['type'] == 'lifespan'
        assert event['type'] == 'lifespan.shutdown'
        self.shutdown_succeeded = True


class TestJar:
    async def on_get(self, req, resp):
        # NOTE(myusko): In the future we shouldn't change the cookie
        #             a test depends on the input.
        # NOTE(kgriffs): This is the only test that uses a single
        #   cookie (vs. multiple) as input; if this input ever changes,
        #   a separate test will need to be added to explicitly verify
        #   this use case.
        resp.set_cookie('has_permission', 'true')

    async def on_post(self, req, resp):
        if req.cookies['has_permission'] == 'true':
            resp.status = falcon.HTTP_200
        else:
            resp.status = falcon.HTTP_403


class WSOptions:
    _SUPPORTED_KEYS = frozenset(
        {'default_close_reasons', 'error_close_code', 'max_receive_queue'}
    )

    def __init__(self, ws_options):
        self._ws_options = ws_options

    async def on_get(self, req, resp):
        resp.media = {
            key: getattr(self._ws_options, key) for key in self._SUPPORTED_KEYS
        }

    async def on_patch(self, req, resp):
        update = await req.get_media()
        for key, value in update.items():
            if key not in self._SUPPORTED_KEYS:
                raise falcon.HTTPInvalidParam('unsupported option', key)
            setattr(self._ws_options, key, value)

        resp.status = falcon.HTTP_NO_CONTENT


def create_app():
    app = falcon.asgi.App()
    bucket = Bucket()
    lifespan_handler = LifespanHandler()

    app.add_route('/', Things())
    app.add_route('/bucket', bucket)
    app.add_route('/bucket/drops', bucket, suffix='drops')
    app.add_route('/events', Events())
    app.add_route('/forms', Multipart())
    app.add_route('/jars', TestJar())
    app.add_route('/feeds/{feed_id}', Feed())
    app.add_route('/wsoptions', WSOptions(app.ws_options))

    app.add_middleware(lifespan_handler)

    async def _on_ws_error(req, resp, error, params, ws=None):
        if not ws:
            raise

        if ws.unaccepted:
            await ws.accept()

        if not ws.closed:
            await ws.send_text(error.__class__.__name__)
            await ws.close()

    app.add_error_handler(falcon.errors.OperationNotAllowed, _on_ws_error)
    app.add_error_handler(ValueError, _on_ws_error)

    return app


application = create_app()
