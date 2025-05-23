import json

import pytest

import falcon
import falcon.errors
import falcon.testing as testing
from falcon.util.deprecation import DeprecatedWarning
from falcon.util.misc import _utcnow

_EXPECTED_BODY = {'status': 'ok'}

context = {'executed_methods': []}  # type: ignore
TEST_ROUTE = '/test_path'


class CaptureResponseMiddleware:
    def process_response(self, req, resp, resource, req_succeeded):
        self.req = req
        self.resp = resp
        self.resource = resource
        self.req_succeeded = req_succeeded


class CaptureRequestMiddleware:
    def process_request(self, req, resp):
        self.req = req


class RequestTimeMiddleware:
    def process_request(self, req, resp):
        global context
        context['start_time'] = _utcnow()

    def process_resource(self, req, resp, resource, params):
        global context
        context['mid_time'] = _utcnow()

    def process_response(self, req, resp, resource, req_succeeded):
        global context
        context['end_time'] = _utcnow()
        context['req_succeeded'] = req_succeeded

    async def process_request_async(self, req, resp):
        self.process_request(req, resp)

    async def process_resource_async(self, req, resp, resource, params):
        self.process_resource(req, resp, resource, params)

    async def process_response_async(self, req, resp, resource, req_succeeded):
        self.process_response(req, resp, resource, req_succeeded)


class TransactionIdMiddleware:
    def process_request(self, req, resp):
        global context
        context['transaction_id'] = 'unique-req-id'

    def process_resource(self, req, resp, resource, params):
        global context
        context['resource_transaction_id'] = 'unique-req-id-2'

    def process_response(self, req, resp, resource, req_succeeded):
        pass


class TransactionIdMiddlewareAsync:
    def __init__(self):
        self._mw = TransactionIdMiddleware()

    async def process_request(self, req, resp):
        self._mw.process_request(req, resp)

    async def process_resource(self, req, resp, resource, params):
        self._mw.process_resource(req, resp, resource, params)

    async def process_response(self, req, resp, resource, req_succeeded):
        self._mw.process_response(req, resp, resource, req_succeeded)


class ExecutedFirstMiddleware:
    def process_request(self, req, resp):
        global context
        context['executed_methods'].append(
            '{}.{}'.format(self.__class__.__name__, 'process_request')
        )

    def process_resource(self, req, resp, resource, params):
        global context
        context['executed_methods'].append(
            '{}.{}'.format(self.__class__.__name__, 'process_resource')
        )

    # NOTE(kgriffs): This also tests that the framework can continue to
    # call process_response() methods that do not have a 'req_succeeded'
    # arg.
    def process_response(self, req, resp, resource, req_succeeded):
        global context
        context['executed_methods'].append(
            '{}.{}'.format(self.__class__.__name__, 'process_response')
        )

        context['req'] = req
        context['resp'] = resp
        context['resource'] = resource


class ExecutedLastMiddleware(ExecutedFirstMiddleware):
    pass


class RemoveBasePathMiddleware:
    def process_request(self, req, resp):
        req.path = req.path.replace('/base_path', '', 1)


class ResponseCacheMiddleware:
    PROCESS_REQUEST_CACHED_BODY = {'cached': True}
    PROCESS_RESOURCE_CACHED_BODY = {'cached': True, 'resource': True}

    def process_request(self, req, resp):
        if req.path == '/cached':
            resp.media = self.PROCESS_REQUEST_CACHED_BODY
            resp.complete = True
            return

    def process_resource(self, req, resp, resource, params):
        if req.path == '/cached/resource':
            resp.media = self.PROCESS_RESOURCE_CACHED_BODY
            resp.complete = True
            return


class AccessParamsMiddleware:
    def process_resource(self, req, resp, resource, params):
        global context
        params['added'] = True
        context['params'] = params


class MiddlewareClassResource:
    def on_get(self, req, resp, **kwargs):
        resp.status = falcon.HTTP_200
        resp.text = json.dumps(_EXPECTED_BODY)

    def on_post(self, req, resp):
        raise falcon.HTTPForbidden(title=falcon.HTTP_403, description='Setec Astronomy')


class EmptySignatureMiddleware:
    def process_request(self):
        pass

    def process_response(self):
        pass


class TestCorsResource:
    def on_get(self, req, resp, **kwargs):
        resp.status = falcon.HTTP_200
        resp.text = 'Test'


class TestMiddleware:
    def setup_method(self, method):
        # Clear context
        global context
        context = {'executed_methods': []}


class TestRequestTimeMiddleware(TestMiddleware):
    def test_skip_process_resource(self, asgi, util):
        global context
        app = util.create_app(asgi, middleware=[RequestTimeMiddleware()])

        app.add_route('/', MiddlewareClassResource())
        client = testing.TestClient(app)

        response = client.simulate_request(path='/404')
        assert response.status == falcon.HTTP_404
        assert 'start_time' in context
        assert 'mid_time' not in context
        assert 'end_time' in context
        assert not context['req_succeeded']

    def test_add_invalid_middleware(self, asgi, util):
        """Test than an invalid class can not be added as middleware"""

        class InvalidMiddleware:
            def process_request(self, *args):
                pass

        mw_list = [RequestTimeMiddleware(), InvalidMiddleware]
        with pytest.raises(AttributeError):
            util.create_app(asgi, middleware=mw_list)

        mw_list = [RequestTimeMiddleware(), 'InvalidMiddleware']
        with pytest.raises(TypeError):
            util.create_app(asgi, middleware=mw_list)

        mw_list = [{'process_request': 90}]
        with pytest.raises(TypeError):
            util.create_app(asgi, middleware=mw_list)

    def test_response_middleware_raises_exception(self, asgi, util):
        """Test that error in response middleware is propagated up"""

        class RaiseErrorMiddleware:
            def process_response(self, req, resp, resource):
                raise Exception('Always fail')

        app = util.create_app(asgi, middleware=[RaiseErrorMiddleware()])

        app.add_route(TEST_ROUTE, MiddlewareClassResource())
        client = testing.TestClient(app)

        result = client.simulate_request(path=TEST_ROUTE)
        assert result.status_code == 500

    @pytest.mark.parametrize('independent_middleware', [True, False])
    def test_log_get_request(self, independent_middleware, asgi, util):
        """Test that Log middleware is executed"""
        global context
        app = util.create_app(
            asgi,
            middleware=[RequestTimeMiddleware()],
            independent_middleware=independent_middleware,
        )

        app.add_route(TEST_ROUTE, MiddlewareClassResource())
        client = testing.TestClient(app)

        response = client.simulate_request(path=TEST_ROUTE)
        assert _EXPECTED_BODY == response.json
        assert response.status == falcon.HTTP_200

        assert 'start_time' in context
        assert 'mid_time' in context
        assert 'end_time' in context
        assert context['mid_time'] >= context['start_time'], (
            'process_resource not executed after request'
        )
        assert context['end_time'] >= context['start_time'], (
            'process_response not executed after request'
        )

        assert context['req_succeeded']


class TestTransactionIdMiddleware(TestMiddleware):
    def test_generate_trans_id_with_request(self, asgi, util):
        """Test that TransactionIdmiddleware is executed"""
        global context

        middleware = (
            TransactionIdMiddlewareAsync() if asgi else TransactionIdMiddleware()
        )
        app = util.create_app(asgi, middleware=middleware)

        app.add_route(TEST_ROUTE, MiddlewareClassResource())
        client = testing.TestClient(app)

        response = client.simulate_request(path=TEST_ROUTE)
        assert _EXPECTED_BODY == response.json
        assert response.status == falcon.HTTP_200
        assert 'transaction_id' in context
        assert 'unique-req-id' == context['transaction_id']


class TestSeveralMiddlewares(TestMiddleware):
    @pytest.mark.parametrize('independent_middleware', [True, False])
    def test_generate_trans_id_and_time_with_request(
        self, independent_middleware, asgi, util
    ):
        # NOTE(kgriffs): We test both so that we can cover the code paths
        # where only a single middleware method is implemented by a
        # component.
        creq = CaptureRequestMiddleware()
        cresp = CaptureResponseMiddleware()

        global context
        app = util.create_app(
            asgi,
            independent_middleware=independent_middleware,
            # NOTE(kgriffs): Pass as a generic iterable to verify that works.
            middleware=iter(
                [
                    TransactionIdMiddleware(),
                    RequestTimeMiddleware(),
                ]
            ),
        )

        # NOTE(kgriffs): Add a couple more after the fact to test
        #   add_middleware().
        app.add_middleware(creq)
        app.add_middleware(cresp)

        app.add_route(TEST_ROUTE, MiddlewareClassResource())
        client = testing.TestClient(app)

        response = client.simulate_request(path=TEST_ROUTE)
        assert _EXPECTED_BODY == response.json
        assert response.status == falcon.HTTP_200
        assert 'transaction_id' in context
        assert 'unique-req-id' == context['transaction_id']
        assert 'start_time' in context
        assert 'mid_time' in context
        assert 'end_time' in context
        assert context['mid_time'] >= context['start_time'], (
            'process_resource not executed after request'
        )
        assert context['end_time'] >= context['start_time'], (
            'process_response not executed after request'
        )

    def test_legacy_middleware_called_with_correct_args(self, asgi, util):
        global context
        app = util.create_app(asgi, middleware=[ExecutedFirstMiddleware()])
        app.add_route(TEST_ROUTE, MiddlewareClassResource())
        client = testing.TestClient(app)

        client.simulate_request(path=TEST_ROUTE)
        assert isinstance(context['req'], falcon.Request)
        assert isinstance(context['resp'], falcon.Response)
        assert isinstance(context['resource'], MiddlewareClassResource)

    def test_middleware_execution_order(self, asgi, util):
        global context
        app = util.create_app(
            asgi,
            independent_middleware=False,
            middleware=[ExecutedFirstMiddleware(), ExecutedLastMiddleware()],
        )

        app.add_route(TEST_ROUTE, MiddlewareClassResource())
        client = testing.TestClient(app)

        response = client.simulate_request(path=TEST_ROUTE)
        assert _EXPECTED_BODY == response.json
        assert response.status == falcon.HTTP_200
        # as the method registration is in a list, the order also is
        # tested
        expectedExecutedMethods = [
            'ExecutedFirstMiddleware.process_request',
            'ExecutedLastMiddleware.process_request',
            'ExecutedFirstMiddleware.process_resource',
            'ExecutedLastMiddleware.process_resource',
            'ExecutedLastMiddleware.process_response',
            'ExecutedFirstMiddleware.process_response',
        ]
        assert expectedExecutedMethods == context['executed_methods']

    def test_independent_middleware_execution_order(self, asgi, util):
        global context
        app = util.create_app(
            asgi,
            independent_middleware=True,
            middleware=[ExecutedFirstMiddleware(), ExecutedLastMiddleware()],
        )

        app.add_route(TEST_ROUTE, MiddlewareClassResource())
        client = testing.TestClient(app)

        response = client.simulate_request(path=TEST_ROUTE)
        assert _EXPECTED_BODY == response.json
        assert response.status == falcon.HTTP_200
        # as the method registration is in a list, the order also is
        # tested
        expectedExecutedMethods = [
            'ExecutedFirstMiddleware.process_request',
            'ExecutedLastMiddleware.process_request',
            'ExecutedFirstMiddleware.process_resource',
            'ExecutedLastMiddleware.process_resource',
            'ExecutedLastMiddleware.process_response',
            'ExecutedFirstMiddleware.process_response',
        ]
        assert expectedExecutedMethods == context['executed_methods']

    def test_multiple_response_mw_throw_exception(self, asgi, util):
        """Test that error in inner middleware leaves"""
        global context

        context['req_succeeded'] = []

        class RaiseStatusMiddleware:
            def process_response(self, req, resp, resource, req_succeeded):
                raise falcon.HTTPStatus(falcon.HTTP_201)

        class RaiseErrorMiddleware:
            def process_response(self, req, resp, resource, req_succeeded):
                raise falcon.HTTPError(falcon.HTTP_748)

        class ProcessResponseMiddleware:
            def process_response(self, req, resp, resource, req_succeeded):
                context['executed_methods'].append('process_response')
                context['req_succeeded'].append(req_succeeded)

        app = util.create_app(
            asgi,
            middleware=[
                ProcessResponseMiddleware(),
                RaiseErrorMiddleware(),
                ProcessResponseMiddleware(),
                RaiseStatusMiddleware(),
                ProcessResponseMiddleware(),
            ],
        )

        app.add_route(TEST_ROUTE, MiddlewareClassResource())
        client = testing.TestClient(app)

        response = client.simulate_request(path=TEST_ROUTE)

        assert response.status == falcon.HTTP_748

        expected_methods = ['process_response'] * 3
        assert context['executed_methods'] == expected_methods
        assert context['req_succeeded'] == [True, False, False]

    def test_inner_mw_throw_exception(self, asgi, util):
        """Test that error in inner middleware leaves"""
        global context

        class MyException(Exception):
            pass

        class RaiseErrorMiddleware:
            def process_request(self, req, resp):
                raise MyException('Always fail')

        app = util.create_app(
            asgi,
            middleware=[
                TransactionIdMiddleware(),
                RequestTimeMiddleware(),
                RaiseErrorMiddleware(),
            ],
        )

        # NOTE(kgriffs): Now that we install a default handler for
        #   Exception, we have to clear them to test the path we want
        #   to trigger with RaiseErrorMiddleware
        # TODO(kgriffs): Since we always add a default error handler
        #   for Exception, should we take out the checks in the WSGI/ASGI
        #   callable and just always assume it will be handled? If so,
        #   then we would remove the test here...
        app._error_handlers.clear()

        app.add_route(TEST_ROUTE, MiddlewareClassResource())
        client = testing.TestClient(app)

        with pytest.raises(MyException):
            client.simulate_request(path=TEST_ROUTE)

        # RequestTimeMiddleware process_response should be executed
        assert 'transaction_id' in context
        assert 'start_time' in context
        assert 'mid_time' not in context

        # NOTE(kgriffs): Should not have been added since raising an
        #   unhandled error skips further processing, including response
        #   middleware methods.
        assert 'end_time' not in context

    def test_inner_mw_throw_exception_while_processing_resp(self, asgi, util):
        """Test that error in inner middleware leaves"""
        global context

        class MyException(Exception):
            pass

        class RaiseErrorMiddleware:
            def process_response(self, req, resp, resource, req_succeeded):
                raise MyException('Always fail')

        app = util.create_app(
            asgi,
            middleware=[
                TransactionIdMiddleware(),
                RequestTimeMiddleware(),
                RaiseErrorMiddleware(),
            ],
        )

        # NOTE(kgriffs): Now that we install a default handler for
        #   Exception, we have to clear them to test the path we want
        #   to trigger with RaiseErrorMiddleware
        # TODO(kgriffs): Since we always add a default error handler
        #   for Exception, should we take out the checks in the WSGI/ASGI
        #   callable and just always assume it will be handled? If so,
        #   then we would remove the test here...
        app._error_handlers.clear()

        app.add_route(TEST_ROUTE, MiddlewareClassResource())
        client = testing.TestClient(app)

        with pytest.raises(MyException):
            client.simulate_request(path=TEST_ROUTE)

        # RequestTimeMiddleware process_response should be executed
        assert 'transaction_id' in context
        assert 'start_time' in context
        assert 'mid_time' in context

        # NOTE(kgriffs): Should not have been added since raising an
        #   unhandled error skips further processing, including response
        #   middleware methods.
        assert 'end_time' not in context

    def test_inner_mw_with_ex_handler_throw_exception(self, asgi, util):
        """Test that error in inner middleware leaves"""
        global context

        class RaiseErrorMiddleware:
            def process_request(self, req, resp, resource):
                raise Exception('Always fail')

        app = util.create_app(
            asgi,
            middleware=[
                TransactionIdMiddleware(),
                RequestTimeMiddleware(),
                RaiseErrorMiddleware(),
            ],
        )

        def handler(req, resp, ex, params):
            context['error_handler'] = True

        app.add_error_handler(Exception, handler)

        app.add_route(TEST_ROUTE, MiddlewareClassResource())
        client = testing.TestClient(app)

        client.simulate_request(path=TEST_ROUTE)

        # RequestTimeMiddleware process_response should be executed
        assert 'transaction_id' in context
        assert 'start_time' in context
        assert 'mid_time' not in context
        assert 'end_time' in context
        assert 'error_handler' in context

    def test_outer_mw_with_ex_handler_throw_exception(self, asgi, util):
        """Test that error in inner middleware leaves"""
        global context

        class RaiseErrorMiddleware:
            def process_request(self, req, resp):
                raise Exception('Always fail')

        app = util.create_app(
            asgi,
            middleware=[
                TransactionIdMiddleware(),
                RaiseErrorMiddleware(),
                RequestTimeMiddleware(),
            ],
        )

        def handler(req, resp, ex, params):
            context['error_handler'] = True

        app.add_error_handler(Exception, handler)

        app.add_route(TEST_ROUTE, MiddlewareClassResource())
        client = testing.TestClient(app)

        client.simulate_request(path=TEST_ROUTE)

        # Any mw is executed now...
        assert 'transaction_id' in context
        assert 'start_time' not in context
        assert 'mid_time' not in context
        assert 'end_time' in context
        assert 'error_handler' in context

    def test_order_mw_executed_when_exception_in_resp(self, asgi, util):
        """Test that error in inner middleware leaves"""
        global context

        class RaiseErrorMiddleware:
            def process_response(self, req, resp, resource):
                raise Exception('Always fail')

        app = util.create_app(
            asgi,
            middleware=[
                ExecutedFirstMiddleware(),
                RaiseErrorMiddleware(),
                ExecutedLastMiddleware(),
            ],
        )

        def handler(req, resp, ex, params):
            pass

        app.add_error_handler(Exception, handler)

        app.add_route(TEST_ROUTE, MiddlewareClassResource())
        client = testing.TestClient(app)

        client.simulate_request(path=TEST_ROUTE)

        # Any mw is executed now...
        expectedExecutedMethods = [
            'ExecutedFirstMiddleware.process_request',
            'ExecutedLastMiddleware.process_request',
            'ExecutedFirstMiddleware.process_resource',
            'ExecutedLastMiddleware.process_resource',
            'ExecutedLastMiddleware.process_response',
            'ExecutedFirstMiddleware.process_response',
        ]
        assert expectedExecutedMethods == context['executed_methods']

    def test_order_independent_mw_executed_when_exception_in_resp(self, asgi, util):
        """Test that error in inner middleware leaves"""
        global context

        class RaiseErrorMiddleware:
            def process_response(self, req, resp, resource):
                raise Exception('Always fail')

        app = util.create_app(
            asgi,
            independent_middleware=True,
            middleware=[
                ExecutedFirstMiddleware(),
                RaiseErrorMiddleware(),
                ExecutedLastMiddleware(),
            ],
        )

        def handler(req, resp, ex, params):
            pass

        app.add_error_handler(Exception, handler)

        app.add_route(TEST_ROUTE, MiddlewareClassResource())
        client = testing.TestClient(app)

        client.simulate_request(path=TEST_ROUTE)

        # Any mw is executed now...
        expectedExecutedMethods = [
            'ExecutedFirstMiddleware.process_request',
            'ExecutedLastMiddleware.process_request',
            'ExecutedFirstMiddleware.process_resource',
            'ExecutedLastMiddleware.process_resource',
            'ExecutedLastMiddleware.process_response',
            'ExecutedFirstMiddleware.process_response',
        ]
        assert expectedExecutedMethods == context['executed_methods']

    def test_order_mw_executed_when_exception_in_req(self, asgi, util):
        """Test that error in inner middleware leaves"""
        global context

        class RaiseErrorMiddleware:
            def process_request(self, req, resp):
                raise Exception('Always fail')

        class RaiseErrorMiddlewareAsync:
            async def process_request(self, req, resp):
                raise Exception('Always fail')

        rem = RaiseErrorMiddlewareAsync() if asgi else RaiseErrorMiddleware()

        app = util.create_app(
            asgi, middleware=[ExecutedFirstMiddleware(), rem, ExecutedLastMiddleware()]
        )

        def handler(req, resp, ex, params):
            pass

        app.add_error_handler(Exception, handler)

        app.add_route(TEST_ROUTE, MiddlewareClassResource())
        client = testing.TestClient(app)

        client.simulate_request(path=TEST_ROUTE)

        # Any mw is executed now...
        expectedExecutedMethods = [
            'ExecutedFirstMiddleware.process_request',
            'ExecutedLastMiddleware.process_response',
            'ExecutedFirstMiddleware.process_response',
        ]
        assert expectedExecutedMethods == context['executed_methods']

    def test_order_independent_mw_executed_when_exception_in_req(self, asgi, util):
        """Test that error in inner middleware leaves"""
        global context

        class RaiseErrorMiddleware:
            def process_request(self, req, resp):
                raise Exception('Always fail')

        class RaiseErrorMiddlewareAsync:
            async def process_request(self, req, resp):
                raise Exception('Always fail')

        rem = RaiseErrorMiddlewareAsync() if asgi else RaiseErrorMiddleware()

        app = util.create_app(
            asgi,
            independent_middleware=True,
            middleware=[ExecutedFirstMiddleware(), rem, ExecutedLastMiddleware()],
        )

        def handler(req, resp, ex, params):
            pass

        app.add_error_handler(Exception, handler)

        app.add_route(TEST_ROUTE, MiddlewareClassResource())
        client = testing.TestClient(app)

        client.simulate_request(path=TEST_ROUTE)

        # All response middleware still executed...
        expectedExecutedMethods = [
            'ExecutedFirstMiddleware.process_request',
            'ExecutedLastMiddleware.process_response',
            'ExecutedFirstMiddleware.process_response',
        ]
        assert expectedExecutedMethods == context['executed_methods']

    def test_order_mw_executed_when_exception_in_rsrc(self, asgi, util):
        """Test that error in inner middleware leaves"""
        global context

        class RaiseErrorMiddleware:
            def process_resource(self, req, resp, resource):
                raise Exception('Always fail')

        class RaiseErrorMiddlewareAsync:
            # NOTE(kgriffs): The *_async postfix is not required in this
            #   case, but we include it to make sure it works as expected.
            async def process_resource_async(self, req, resp, resource):
                raise Exception('Always fail')

        rem = RaiseErrorMiddlewareAsync() if asgi else RaiseErrorMiddleware()

        app = util.create_app(
            asgi, middleware=[ExecutedFirstMiddleware(), rem, ExecutedLastMiddleware()]
        )

        def handler(req, resp, ex, params):
            pass

        app.add_error_handler(Exception, handler)

        app.add_route(TEST_ROUTE, MiddlewareClassResource())
        client = testing.TestClient(app)

        client.simulate_request(path=TEST_ROUTE)

        # Any mw is executed now...
        expectedExecutedMethods = [
            'ExecutedFirstMiddleware.process_request',
            'ExecutedLastMiddleware.process_request',
            'ExecutedFirstMiddleware.process_resource',
            'ExecutedLastMiddleware.process_response',
            'ExecutedFirstMiddleware.process_response',
        ]
        assert expectedExecutedMethods == context['executed_methods']

    def test_order_independent_mw_executed_when_exception_in_rsrc(self, asgi, util):
        """Test that error in inner middleware leaves"""
        global context

        class RaiseErrorMiddleware:
            def process_resource(self, req, resp, resource):
                raise Exception('Always fail')

        class RaiseErrorMiddlewareAsync:
            async def process_resource(self, req, resp, resource):
                raise Exception('Always fail')

        rem = RaiseErrorMiddlewareAsync() if asgi else RaiseErrorMiddleware()

        app = util.create_app(
            asgi,
            independent_middleware=True,
            middleware=[ExecutedFirstMiddleware(), rem, ExecutedLastMiddleware()],
        )

        def handler(req, resp, ex, params):
            pass

        app.add_error_handler(Exception, handler)

        app.add_route(TEST_ROUTE, MiddlewareClassResource())
        client = testing.TestClient(app)

        client.simulate_request(path=TEST_ROUTE)

        # Any mw is executed now...
        expectedExecutedMethods = [
            'ExecutedFirstMiddleware.process_request',
            'ExecutedLastMiddleware.process_request',
            'ExecutedFirstMiddleware.process_resource',
            'ExecutedLastMiddleware.process_response',
            'ExecutedFirstMiddleware.process_response',
        ]
        assert expectedExecutedMethods == context['executed_methods']


class TestRemoveBasePathMiddleware(TestMiddleware):
    def test_base_path_is_removed_before_routing(self, asgi, util):
        """Test that RemoveBasePathMiddleware is executed before routing"""
        app = util.create_app(asgi, middleware=RemoveBasePathMiddleware())

        # We dont include /base_path as it will be removed in middleware
        app.add_route('/sub_path', MiddlewareClassResource())
        client = testing.TestClient(app)

        response = client.simulate_request(path='/base_path/sub_path')
        assert _EXPECTED_BODY == response.json
        assert response.status == falcon.HTTP_200
        response = client.simulate_request(path='/base_pathIncorrect/sub_path')
        assert response.status == falcon.HTTP_404


class TestResourceMiddleware(TestMiddleware):
    @pytest.mark.parametrize('independent_middleware', [True, False])
    def test_can_access_resource_params(self, asgi, util, independent_middleware):
        """Test that params can be accessed from within process_resource"""
        global context

        class Resource:
            def on_get(self, req, resp, **params):
                resp.text = json.dumps(params)

        app = util.create_app(
            asgi,
            middleware=AccessParamsMiddleware(),
            independent_middleware=independent_middleware,
        )
        app.add_route('/path/{id}', Resource())
        client = testing.TestClient(app)
        response = client.simulate_request(path='/path/22')

        assert 'params' in context
        assert context['params']
        assert context['params']['id'] == '22'
        assert response.json == {'added': True, 'id': '22'}


class TestEmptySignatureMiddleware(TestMiddleware):
    def test_dont_need_params_in_signature(self, asgi, util):
        """
        Verify that we don't need parameters in the process_* signatures (for
        side-effect-only middlewares, mostly). Makes no difference on py27
        but does affect py36.

        https://github.com/falconry/falcon/issues/1254
        """
        util.create_app(asgi, middleware=EmptySignatureMiddleware())


class TestErrorHandling(TestMiddleware):
    def test_error_composed_before_resp_middleware_called(self, asgi, util):
        mw = CaptureResponseMiddleware()
        app = util.create_app(asgi, middleware=mw)
        app.add_route('/', MiddlewareClassResource())
        client = testing.TestClient(app)

        response = client.simulate_request(path='/', method='POST')
        assert response.status == falcon.HTTP_403
        assert mw.resp.status == response.status

        composed_body = json.loads(mw.resp.data.decode())
        assert composed_body['title'] == response.status

        assert not mw.req_succeeded

        # NOTE(kgriffs): Sanity-check the other params passed to
        # process_response()
        assert isinstance(mw.req, falcon.Request)
        assert isinstance(mw.resource, MiddlewareClassResource)

    def test_http_status_raised_from_error_handler(self, asgi, util):
        mw = CaptureResponseMiddleware()
        app = util.create_app(asgi, middleware=mw)
        app.add_route('/', MiddlewareClassResource())
        client = testing.TestClient(app)

        # NOTE(kgriffs): Use the old-style error handler signature to
        #   ensure our shim for that works as expected.
        def _http_error_handler(error, req, resp, params):
            raise falcon.HTTPStatus(falcon.HTTP_201)

        async def _http_error_handler_async(req, resp, error, params):
            raise falcon.HTTPStatus(falcon.HTTP_201)

        h = _http_error_handler_async if asgi else _http_error_handler

        # NOTE(kgriffs): This will take precedence over the default
        # handler for facon.HTTPError.
        if asgi:
            # NOTE(vytas): The ASGI flavour supports no reordering shim.
            app.add_error_handler(falcon.HTTPError, h)
        else:
            with pytest.warns(DeprecatedWarning, match='deprecated signature'):
                app.add_error_handler(falcon.HTTPError, h)

        response = client.simulate_request(path='/', method='POST')
        assert response.status == falcon.HTTP_201
        assert mw.resp.status == response.status


class TestShortCircuiting(TestMiddleware):
    def setup_method(self, method):
        super(TestShortCircuiting, self).setup_method(method)

    def _make_client(self, asgi, util, independent_middleware=True):
        mw = [
            RequestTimeMiddleware(),
            ResponseCacheMiddleware(),
            TransactionIdMiddleware(),
        ]
        app = util.create_app(
            asgi, middleware=mw, independent_middleware=independent_middleware
        )
        app.add_route('/', MiddlewareClassResource())
        app.add_route('/cached', MiddlewareClassResource())
        app.add_route('/cached/resource', MiddlewareClassResource())

        return testing.TestClient(app)

    def test_process_request_not_cached(self, asgi, util):
        response = self._make_client(asgi, util).simulate_get('/')
        assert response.status == falcon.HTTP_200
        assert response.json == _EXPECTED_BODY
        assert 'transaction_id' in context
        assert 'resource_transaction_id' in context
        assert 'mid_time' in context
        assert 'end_time' in context

    @pytest.mark.parametrize('independent_middleware', [True, False])
    def test_process_request_cached(self, asgi, util, independent_middleware):
        response = self._make_client(asgi, util, independent_middleware).simulate_get(
            '/cached'
        )
        assert response.status == falcon.HTTP_200
        assert response.json == ResponseCacheMiddleware.PROCESS_REQUEST_CACHED_BODY

        # NOTE(kgriffs): Since TransactionIdMiddleware was ordered after
        # ResponseCacheMiddleware, the response short-circuiting should have
        # skipped it.
        assert 'transaction_id' not in context
        assert 'resource_transaction_id' not in context

        # NOTE(kgriffs): RequestTimeMiddleware only adds this in
        # process_resource(), which should be skipped when
        # ResponseCacheMiddleware sets resp.completed = True in
        # process_request().
        assert 'mid_time' not in context

        # NOTE(kgriffs): Short-circuiting does not affect process_response()
        assert 'end_time' in context

    @pytest.mark.parametrize('independent_middleware', [True, False])
    def test_process_resource_cached(self, asgi, util, independent_middleware):
        response = self._make_client(asgi, util, independent_middleware).simulate_get(
            '/cached/resource'
        )
        assert response.status == falcon.HTTP_200
        assert response.json == ResponseCacheMiddleware.PROCESS_RESOURCE_CACHED_BODY

        # NOTE(kgriffs): This should be present because it is added in
        # process_request(), but the short-circuit does not occur until
        # process_resource().
        assert 'transaction_id' in context

        # NOTE(kgriffs): Since TransactionIdMiddleware was ordered after
        # ResponseCacheMiddleware, the response short-circuiting should have
        # skipped it.
        assert 'resource_transaction_id' not in context

        # NOTE(kgriffs): RequestTimeMiddleware only adds this in
        # process_resource(), which will not be skipped in this case because
        # RequestTimeMiddleware is ordered before ResponseCacheMiddleware.
        assert 'mid_time' in context

        # NOTE(kgriffs): Short-circuiting does not affect process_response()
        assert 'end_time' in context


class TestCORSMiddlewareWithAnotherMiddleware(TestMiddleware):
    @pytest.mark.parametrize(
        'mw,allowed',
        [
            (CaptureResponseMiddleware(), True),
            ([CaptureResponseMiddleware()], True),
            ((CaptureResponseMiddleware(),), True),
            (iter([CaptureResponseMiddleware()]), True),
            (falcon.CORSMiddleware(), False),
            ([falcon.CORSMiddleware()], False),
        ],
    )
    def test_api_initialization_with_cors_enabled_and_middleware_param(
        self, mw, asgi, util, allowed
    ):
        if allowed:
            app = util.create_app(asgi, middleware=mw, cors_enable=True)
            app.add_route('/', TestCorsResource())
            client = testing.TestClient(app)
            result = client.simulate_get(headers={'Origin': 'localhost'})
            assert result.headers['Access-Control-Allow-Origin'] == '*'
        else:
            with pytest.raises(ValueError, match='CORSMiddleware'):
                app = util.create_app(asgi, middleware=mw, cors_enable=True)


def test_async_postfix_method_must_be_coroutine(util):
    class FaultyComponentA:
        def process_request_async(self, req, resp):
            pass

    class FaultyComponentB:
        def process_resource_async(self, req, resp, resource, params):
            pass

    class FaultyComponentC:
        def process_response_async(self, req, resp, resource, req_succeeded):
            pass

    if util.HAS_CYTHON:
        pytest.skip(reason='Cythonized coroutine functions cannot be detected')

    for mw in (FaultyComponentA, FaultyComponentB, FaultyComponentC):
        with pytest.raises(falcon.errors.CompatibilityError):
            util.create_app(True, middleware=[mw()])
