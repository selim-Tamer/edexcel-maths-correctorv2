"""In-process HTTP regressions; no database or external HTTP requests.

Separate from the strict security runner: Windows TestClient's asyncio self-pipe
needs OS socketpair. Never loosen the security runner's network block for it.
"""
import os
import unittest
from unittest import mock
from types import SimpleNamespace


class RemainingApiContractTests(unittest.TestCase):
    def test_real_api_requires_linked_job_and_preserves_auth_order(self):
        from tests import test_postgres_api_persistence as support
        from tests import test_br02_pg_api as live
        from api.app import create_app
        from fastapi.testclient import TestClient
        from membership_store import Membership
        import identity_context,runtime_config
        backend=support._Backend();job_id=None
        def row():
            return dict(support._Backend.row(backend),trusted_job_id=job_id)
        backend.row=row
        jobs=SimpleNamespace(require_connection=lambda:SimpleNamespace(close=lambda:None),
            get_job=lambda *a,**kw:{'status':'succeeded','tenant_id':support.TENANT,
                'owner_subject':support.OWNER,'document_digest':support.DOCUMENT,
                'result_id':support.RESULT})
        def member(subject):
            return Membership(subject,support.TENANT,'teacher') if subject==support.SUBJECT else None
        with mock.patch.dict(os.environ,{runtime_config.RUNTIME_ENV:'production',runtime_config.PG_DSN_ENV:'inert-app'}), \
                mock.patch('api.app._job_api',return_value=jobs), \
                mock.patch('result_service.open_service',side_effect=lambda identity,**kw:support._FakeResultService(backend,identity)):
            app=create_app(verify_token=live._synthetic_api_verify_token,
                membership_resolver=member,
                allowed_origins=['https://127.0.0.1'],engine=None)
            with TestClient(app) as client:
                path='/v1/results/'+support.RESULT+'/decisions'
                body={'question_number':'1','action':'approve','expected_revision':7,'idempotency_key':'offline-key'}
                auth={'Authorization':'Bearer synthetic-subject:'+support.SUBJECT}
                self.assertEqual(client.post(path,headers=auth,json=body).status_code,403)
                job_id='offline-trusted-job'
                self.assertEqual(client.post(path,headers=auth,json=body).status_code,409)
                invalid=client.post(path,headers={'Authorization':'Bearer not-a-synthetic-token'},json=body)
                self.assertEqual(invalid.status_code,401)
                nonmember=client.post(path,headers={'Authorization':'Bearer synthetic-subject:nope'},json=body)
                self.assertEqual(nonmember.status_code,403)
        self.assertEqual(backend.events,[])


    def test_fresh_completion_fixture_reaches_real_write_denial(self):
        from tests import test_br02_pg_api as live
        from tests.test_br02_pg_registration import _RegistrationConnection
        from tests import test_postgres_api_persistence as support
        import br02_pg_fixtures,postgres_result_store,result_service,runtime_config,identity_context
        from api.app import create_app
        from membership_store import Membership
        connection=_RegistrationConnection();connection.close=lambda:None
        case=live.ProductionApiLiveTests('test_production_fresh_completion_is_denied_with_zero_writes')
        case.bundle=br02_pg_fixtures.build_bundle('offline-f5');case.dsn='inert-admin'
        case.approval=SimpleNamespace(app_dsn='inert-app',runtime_login='marker_app_login',target_dsn='inert-admin')
        case.conn=SimpleNamespace(execute=lambda *a:SimpleNamespace(fetchone=lambda:(0,)))
        def production_app():
            return create_app(verify_token=live._synthetic_api_verify_token,
                membership_resolver=lambda s:Membership(s,case.bundle.tenant_id,'teacher'),
                result_service_factory=lambda s,t:support._FakeResultService(support._Backend(),identity_context.from_verified_subject(s,t)),
                allowed_origins=['https://127.0.0.1'],engine=None)
        case.production_app=production_app
        with mock.patch.dict(os.environ,{runtime_config.RUNTIME_ENV:'production',runtime_config.PG_DSN_ENV:'inert-app'}), \
                mock.patch.object(postgres_result_store,'open_store',side_effect=lambda dsn,**kw:postgres_result_store.PostgresResultStore(connection,**kw)), \
                mock.patch.object(result_service,'require_capability',return_value=('1','1')):
            case.test_production_fresh_completion_is_denied_with_zero_writes()
        self.assertEqual(connection.anchors,{})
        self.assertEqual(connection.contexts,{})


