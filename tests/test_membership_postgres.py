"""Ephemeral CI DB only. Never run against a Supabase or production DSN."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DSN = os.environ.get("MEMBERS_TEST_DSN", "")


@unittest.skipUnless(DSN, "requires isolated local PostgreSQL test service")
class MembershipPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        cfg=conninfo_to_dict(DSN)
        if cfg.get("host") not in {"127.0.0.1","localhost"} or cfg.get("dbname") != "members_test":
            raise RuntimeError("Refusing non-local/non-test database")
        cls.connect=staticmethod(lambda: psycopg.connect(DSN,autocommit=True))
        with cls.connect() as c:
            for role in ["anon","authenticated","service_role"]:
                if not c.execute("select 1 from pg_roles where rolname=%s",(role,)).fetchone():
                    c.execute("create role " + role + (" bypassrls" if role=="service_role" else ""))
            c.execute("grant usage on schema public to anon, authenticated, service_role")
            c.execute((Path(__file__).resolve().parents[1]/"migrations/001_members.sql").read_text())

    def setUp(self):
        self.c=self.connect()
        self.addCleanup(self.c.close)
        self.c.execute("truncate public.vd_members")
        self.encoded="scrypt1$"+"0"*32+"$"+"1"*64
        self.id=self.c.execute("select public.vd_member_issue(%s,%s,%s)",("VD-000000000001","test",self.encoded)).fetchone()[0]["id"]

    def attempt(self, device="a", session="b", ok=True, expected=None, connection=None):
        return (connection or self.c).execute("select public.vd_member_attempt(%s,%s,%s,%s,%s)",
          ("VD-000000000001",expected or self.encoded,ok,device*64,session*64)).fetchone()[0]

    def valid(self, device="a", session="b"):
        return self.c.execute("select public.vd_member_session(%s,%s,%s)",(self.id,device*64,session*64)).fetchone()[0]

    def manage(self, action, encoded=None):
        return self.c.execute("select public.vd_member_manage(%s,%s,%s)",(self.id,action,encoded)).fetchone()[0]

    def test_creation_expiry_and_private_overview(self):
        self.assertTrue(self.c.execute("select expires_at-created_at=interval '7 days' from public.vd_members").fetchone()[0])
        out=self.c.execute("select public.vd_member_overview(0)").fetchone()[0]
        self.assertEqual(out["active"],1)
        for key in ("password_hash","device_hash","session_hash"):
            self.assertNotIn(key,str(out))

    def test_first_device_atomic_under_concurrency(self):
        def login(device):
            with self.connect() as c:
                return self.attempt(device=device,connection=c)
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(login,["a","c"]))
        self.assertEqual(sum(r["ok"] for r in results),1)
        self.assertEqual([r["reason"] for r in results if not r["ok"]],["device"])

    def test_expiry_rejects_existing_session_and_new_login(self):
        self.assertTrue(self.attempt()["ok"])
        self.c.execute("update public.vd_members set expires_at=now() where id=%s",(self.id,))
        self.assertIsNone(self.valid())
        self.assertEqual(self.attempt()["reason"],"expired")
        self.manage("renew")
        self.assertTrue(self.c.execute("select expires_at>now()+interval '6 days' from public.vd_members").fetchone()[0])

    def test_active_renewal_adds_seven_days(self):
        before=self.c.execute("select expires_at from public.vd_members").fetchone()[0]
        self.manage("renew")
        after=self.c.execute("select expires_at from public.vd_members").fetchone()[0]
        self.assertEqual((after-before).total_seconds(),7*86400)

    def test_disable_reset_and_delete_revoke_sessions(self):
        self.attempt()
        self.manage("disable")
        self.assertIsNone(self.valid())
        self.assertFalse(self.attempt()["ok"])
        self.manage("renew")
        self.attempt()
        self.manage("reset_device")
        self.assertIsNone(self.valid())
        self.assertTrue(self.attempt(device="c")["ok"])
        self.manage("delete")
        self.assertIsNone(self.valid(device="c"))
        self.assertFalse(self.manage("renew")["ok"])
        out=self.c.execute("select public.vd_member_overview(0)").fetchone()[0]
        self.assertEqual((out["total_issued"],out["current"],out["deleted"]),(1,0,1))
        self.assertEqual(self.c.execute("select password_hash,device_hash,session_hash,label from public.vd_members").fetchone(),(None,None,None,"已刪除"))

    def test_password_reset_race_and_same_device_relogin(self):
        self.attempt()
        new_hash="scrypt1$"+"2"*32+"$"+"3"*64
        self.manage("reset_password",new_hash)
        self.assertIsNone(self.valid())
        self.assertFalse(self.attempt()["ok"])
        self.assertTrue(self.attempt(expected=new_hash)["ok"])
        self.attempt(session="d",expected=new_hash)
        self.assertIsNone(self.valid())
        self.assertTrue(self.valid(session="d")["ok"])

    def test_throttle_survives_sessions_and_expires(self):
        for _ in range(5): self.attempt(ok=False)
        self.assertEqual(self.attempt()["reason"],"locked")
        self.c.execute("update public.vd_members set locked_until=now()-interval '1 second'")
        self.assertTrue(self.attempt()["ok"])

    def test_anon_cannot_read_or_call_authentication_functions(self):
        for role in ("anon","authenticated"):
            self.assertFalse(self.c.execute("select has_table_privilege(%s,'public.vd_members','select')",(role,)).fetchone()[0])
            functions=self.c.execute("select oid from pg_proc where proname like 'vd_member_%'").fetchall()
            for (oid,) in functions:
                self.assertFalse(self.c.execute("select has_function_privilege(%s,%s,'execute')",(role,oid)).fetchone()[0])
        self.c.execute("set role service_role")
        self.assertTrue(self.attempt()["ok"])
        self.c.execute("reset role")

    def test_logout_old_session_cannot_revoke_new_session(self):
        self.attempt()
        self.attempt(session="d")
        self.c.execute("select public.vd_member_logout(%s,%s)",(self.id,"b"*64))
        self.assertTrue(self.valid(session="d")["ok"])
