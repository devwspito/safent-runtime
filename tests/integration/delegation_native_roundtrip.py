"""Disposable --network none image: real EE relay + Community queue + Hermes.

Mount current runtime at /review and EE src at /enterprise-src, read-only.
Only HTTP authentication adapter and LLM answers are fixture code; relay signing,
inbox verification, admission, queue/orchestrator, native SDK, audit, persistence
and status/result services are production. This is not a deployed UI/RBAC test.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

sys.path.insert(0, "/enterprise-src")


def main():  # noqa: PLR0915 — isolated composition smoke owns its complete lifecycle
    from safent_control.application.delegation_service import DelegationService
    from safent_control.application.delegation_status import DelegationStatusService
    from safent_control.domain import crypto
    from safent_control.domain.delegation_status import DelegationStatusEvent
    from safent_control.domain.entities import (
        AgentTemplate,
        Employee,
        Instance,
        InstanceState,
        Tenant,
    )
    from safent_control.infrastructure.keystore import TenantKeystore
    from safent_control.infrastructure.repository import ControlPlaneRepository, hash_secret

    from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
    from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
    from hermes.capabilities.domain.ports import ConsentContext
    from hermes.config_sync import delegation_inbox as inbox
    from hermes.config_sync import delegation_status as status
    from hermes.instance.association_store import InstanceAssociation, SQLiteAssociationStore
    from hermes.prompts.persona import PersonaSpec
    from hermes.runtime.model_config import ModelConfig
    from hermes.runtime.nous_engine import NousReasoningEngine
    from hermes.shell_server.security.secrets import SecretsVault
    from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator
    from hermes.tasks.infrastructure.sqlite_agent_state import SqliteAgentState
    from hermes.tasks.infrastructure.sqlite_conversation_repo import SQLiteConversationRepository
    from hermes.tasks.infrastructure.sqlite_pending_delegations import (
        SqlitePendingDelegationRepository,
    )
    from hermes.tasks.infrastructure.sqlite_task_dashboard import read_task_dashboard
    from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue
    from hermes.tasks.triggers.application.delegation_approval_service import (
        DelegationApprovalService,
    )
    from hermes.tasks.triggers.application.delegation_authority import DelegationAdmissionAuthority
    from hermes.tasks.triggers.application.trigger_gate import TriggerGate
    from hermes.tasks.triggers.infrastructure.sqlite_authorized_trigger_repository import (
        SqliteAuthorizedTriggerRepository,
    )

    with tempfile.TemporaryDirectory(prefix="task-ce-native-") as directory:
        root = Path(directory)
        org, employee, template, instance_id, owner = (str(uuid4()) for _ in range(5))
        secret = "fictional-instance-only"
        central = ControlPlaneRepository(db_path=root / "enterprise.db")
        central.save_tenant(Tenant(org_id=org, name="Fixture", seat_limit=2))
        central.save_employee(
            Employee(
                employee_id=employee, org_id=org, email="worker@example.invalid", name="Fixture"
            )
        )
        central.save_agent_template(
            AgentTemplate(
                agent_template_id=template, employee_id=employee, org_id=org, name="Fixture"
            )
        )
        instance = Instance(
            instance_id=instance_id,
            org_id=org,
            agent_template_id=template,
            hardware_fingerprint="fixture",
            state=InstanceState.ACTIVE,
            last_seen=datetime.now(UTC),
        )
        central.save_instance(instance, instance_secret_hash=hash_secret(secret))
        keys = TenantKeystore(db_path=root / "keys.db", data_dir=root)
        private, public = crypto.generate_signing_keypair()
        keys.store_keypair(org_id=org, private_hex=private, public_hex=public)
        relay = DelegationService(repo=central, keystore=keys)
        telemetry = DelegationStatusService(central)
        request = relay.submit_console_request(
            org_id=org,
            acting_user_id=owner,
            to_employee_id=employee,
            to_agent_id=None,
            body="Reply exactly OK.",
            correlation_id="roundtrip",
            now=datetime.now(UTC),
        )
        native_db = root / "community.db"
        queue = SqliteWorkQueue(db_path=native_db)
        pending = SqlitePendingDelegationRepository(native_db)
        association = SQLiteAssociationStore(
            db_path=native_db,
            vault=SecretsVault(master_key=os.urandom(32)),
        )
        association.save(
            association=InstanceAssociation(
                instance_id=instance_id,
                tenant_id=org,
                paired_at=datetime.now(UTC).isoformat(),
                cloud_endpoint="http://loopback.fixture",
                signing_pubkey_hex=public,
                license={},
                last_applied_version=0,
                state="active",
            ),
            instance_secret=secret,
        )
        conversations = SQLiteConversationRepository(db_path=native_db)
        signer = AuditHashChainSigner(signing_key=os.urandom(32))
        audit = SqliteAuditRepository(db_path=root / "audit.db")
        state = SqliteAgentState(db_path=native_db, signer=signer, audit_repo=audit)
        trigger_connection = sqlite3.connect(native_db, check_same_thread=False)
        triggers = SqliteAuthorizedTriggerRepository(trigger_connection)
        admission = DelegationApprovalService(
            pending_repo=pending,
            trigger_repo=triggers,
            gate=TriggerGate(
                trigger_repo=triggers,
                queue=queue,
                agent_state=state,
                tenant_id=UUID(org),
                audit_signer=signer,
            ),
            conversation_repo=conversations,
            authority=DelegationAdmissionAuthority(
                association_store=association, pending_repo=pending
            ),
        )
        llm_calls = []

        def wire(message):
            result = {key: getattr(message, key) for key in inbox._ENVELOPE_KEYS}
            result["kind"] = message.kind.value
            result["signature_hex"] = message.signature_hex
            return result

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self, body, code=200):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_GET(self):
                if urlsplit(self.path).path != "/v1/inbox":
                    return self.respond({}, 404)  # Native model-discovery probes.
                assert self.headers.get("Authorization") == "Bearer " + secret
                query = parse_qs(urlsplit(self.path).query)
                assert query["instance_id"] == [instance_id]
                rows = relay.list_inbox(
                    to_instance_id=instance_id,
                    since=query.get("since", [""])[0],
                    now=datetime.now(UTC),
                )
                return self.respond({"messages": [wire(row) for row in rows]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                if self.path.endswith("/api/show"):
                    return self.respond({}, 404)
                if self.path == "/llm/chat/completions":
                    assert self.headers.get("Authorization") == "Bearer fictional-llm"
                    assert body["model"] == "fixture" and body.get("stream") is True
                    llm_calls.append(True)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for delta, finish in [
                        ({"role": "assistant", "content": "OK"}, None),
                        ({}, "stop"),
                    ]:
                        frame = {
                            "id": "fixture",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "fixture",
                            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                        }
                        self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    return None
                assert self.headers.get("Authorization") == "Bearer " + secret
                if self.path == "/v1/inbox/ack":
                    return self.respond(
                        {
                            "acked": relay.ack(
                                to_instance_id=instance_id,
                                message_ids=body["message_ids"],
                                now=datetime.now(UTC),
                            )
                        }
                    )
                if self.path == "/v1/outbox/result":
                    message = relay.submit_result(
                        responding_instance=instance,
                        correlation_id=body["correlation_id"],
                        body=body["body"],
                        now=datetime.now(UTC),
                    )
                    return self.respond(
                        {
                            "message_id": message.message_id,
                            "correlation_id": message.correlation_id,
                            "state": message.state.value,
                        },
                        201,
                    )
                assert self.path == "/v1/delegations/" + request.message_id + "/status"
                return self.respond(
                    telemetry.record(
                        instance=instance,
                        request_id=request.message_id,
                        event=DelegationStatusEvent.model_validate(body),
                        expected_secret_hash=hash_secret(secret),
                        now=datetime.now(UTC),
                    )
                )

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        endpoint = f"http://127.0.0.1:{server.server_port}"

        class Proxy:
            async def call_dict(self, member, envelope_json, *_args):
                assert member == "submit_inbound_delegation"
                return {
                    "ok": True,
                    "status": await admission.submit(envelope=json.loads(envelope_json)),
                }

        class NoEffects:
            async def execute(self, *_args, **_kwargs):
                raise AssertionError("Narrative fixture must not execute a tool")

        def flush():
            status.push_status_events(
                native_db,
                instance_id=instance_id,
                cloud_endpoint=endpoint,
                instance_secret=secret,
                is_current=lambda: True,
            )

        def task():
            return relay.list_tasks(org_id=org, now=datetime.now(UTC))["tasks"][0]

        async def run():
            await inbox.poll_and_apply_inbox_once(
                db_path=native_db,
                cloud_endpoint=endpoint,
                instance_id=instance_id,
                instance_secret=secret,
                pubkey_hex=public,
                proxy=Proxy(),
            )
            assert len(admission.list_pending()) == 1
            assert await queue.claim_next() is None and not llm_calls
            flush()
            assert task()["execution_status"] == "awaiting_approval"
            task_id = await admission.approve(
                message_id=request.message_id, approved_by=UUID(owner)
            )
            assert task_id is not None
            flush()
            assert task()["execution_status"] == "queued"
            claimed = await queue.claim_next()
            assert claimed and claimed.id == task_id
            assert claimed.payload["enqueued_by"] == owner
            assert claimed.payload["derived_from_untrusted_content"] is True
            flush()
            assert task()["execution_status"] == "running"
            engine = NousReasoningEngine(
                persona=PersonaSpec(
                    name="Fixture",
                    role="assistant",
                    language="en",
                    register="",
                    primary_mission="Reply OK",
                ),
                enabled_toolsets=[],
                model_config=ModelConfig(
                    model="custom/fixture",
                    native_provider="custom",
                    api_key="fictional-llm",
                    base_url=endpoint + "/llm",
                    max_tokens=16,
                    max_iterations=1,
                ),
            )
            runner = AgentLoopOrchestrator(
                queue=queue,
                state=state,
                engine=engine,
                broker=NoEffects(),
                notify_watchdog=lambda: None,
                consent_context=ConsentContext(tenant_id=UUID(org), operator_id=UUID(owner)),
                firmer=signer,
                audit_repo=audit,
                conversation_repo=conversations,
                memory_extraction_enabled=False,
            )
            await runner.bootstrap()
            await runner._process(claimed)
            local = read_task_dashboard(native_db)["tasks"][0]
            assert local["status"] == "completed", local["status"]
            assert local["result"] == "OK" and llm_calls
            flush()
            assert task()["execution_status"] == "completed" and task()["result"] is None
            inbox.push_pending_delegation_results_once(
                db_path=native_db,
                cloud_endpoint=endpoint,
                instance_secret=secret,
                instance_id=instance_id,
                is_current=lambda: True,
            )
            assert task()["result"]["body"] == "OK"
            assert (
                await admission.approve(message_id=request.message_id, approved_by=UUID(owner))
                is None
            )
            assert await queue.claim_next() is None
            print(
                "PASS TASK-CE: signed EE relay -> local human admission -> "
                "native Hermes/orchestrator -> signed local audit -> "
                "completed status -> result; no replay",
                flush=True,
            )

        try:
            asyncio.run(run())
        finally:
            server.shutdown()
            server.server_close()
            pending._conn.close()
            trigger_connection.close()


if __name__ == "__main__":
    main()
