"""Real Frappe 16 workers, tenant databases and Redis Search acceptance."""
import json
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

import frappe


def wait_for(check, description, seconds=180):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        frappe.db.rollback()
        if check():
            return
        time.sleep(.5)
    raise AssertionError('Timed out: '+description)


def connect(site):
    frappe.destroy()
    frappe.init(site=site, sites_path=os.getcwd())
    frappe.connect()
    frappe.set_user('Administrator')
    frappe.flags.mute_emails=True


def symlink_discovery():
    from frappe.utils import get_sites
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        sites=root/'sites'
        sites.mkdir()
        for name in ('regular.local','unconfigured.local'):
            (sites/name).mkdir()
        (sites/'regular.local/site_config.json').write_text('{}')
        (root/'target').mkdir()
        (root/'target/site_config.json').write_text('{}')
        (sites/'linked.local').symlink_to(root/'target',target_is_directory=True)
        (sites/'broken.local').symlink_to(root/'absent',target_is_directory=True)
        (sites/'cycle.local').symlink_to(sites/'cycle.local',target_is_directory=True)
        for config, env, enabled in [(None,None,False),(False,None,False),(True,None,True),
                                    (False,'1',True),(False,'true',True),(True,'0',False),(True,'false',False)]:
            (sites/'common_site_config.json').write_text(json.dumps({'allow_symlink_sites':config}))
            with patch.dict(os.environ):
                os.environ.pop('FRAPPE_ALLOW_SYMLINK_SITES',None)
                if env is not None:
                    os.environ['FRAPPE_ALLOW_SYMLINK_SITES']=env
                assert get_sites(str(sites))==sorted(['regular.local']+(['linked.local'] if enabled else []))
    assert Path('test.local').is_symlink()
    assert 'test.local' in get_sites(os.getcwd())
    print('PASS: real symlink/default/ENV precedence/broken/cyclic site discovery',flush=True)


def verify_community(session, base):
    from helpdesk.test_utils import create_agent, make_ticket
    from helpdesk.search_redis import RedisSearchBackend
    from helpdesk.ticket_search import get_search_backend, build_index_in_background
    from frappe.utils import add_to_date, now_datetime
    primary='test.local'
    other='search-other.local'
    symlink_discovery()
    namespaces={}
    tickets={}
    token='containerredisacceptance'
    for site in (primary,other):
        connect(site)
        assert type(get_search_backend()).__name__=='RedisSearchBackend'
        backend=RedisSearchBackend()
        namespaces[site]=backend.namespace
        create_agent('same-agent@example.test')
        ticket=make_ticket(subject=token+' '+site,description='Türkçe arama testi İstanbul',save=False)
        ticket.insert(ignore_permissions=True,set_name='900000001')
        tickets[site]=ticket.name
        frappe.db.commit()
        build_index_in_background()
        frappe.db.commit()
        wait_for(lambda: backend.index_exists() and backend.search(token)['results'],site+' worker index')
        assert all(site in row['title'] for row in backend.search(token)['results'])
        frappe.clear_cache()
        assert backend.index_exists(),'Site cache clear erased search index'
        assert backend.search(token)['results']
    assert namespaces[primary]!=namespaces[other]
    assert tickets[primary]==tickets[other], 'Isolation fixture must use colliding ticket IDs'
    other_backend=backend
    other_ready=backend.redis.execute_command('GET',backend.ready)
    connect(primary)
    backend=RedisSearchBackend()
    import requests
    endpoint=base+'/api/method/helpdesk.api.search.'
    for method in ('search', 'get_filter_options'):
        params={'query': token} if method=='search' else {}
        response=session.get(endpoint+method, params=params, timeout=30)
        response.raise_for_status()
        anonymous=requests.get(endpoint+method, params=params, headers={'Host': primary}, timeout=30)
        assert anonymous.status_code in (401,403), 'Guest could access ticket search API'
    from frappe.utils.scheduler import is_scheduler_inactive
    for flag in ('pause_scheduler', 'disable_scheduler', 'maintenance_mode'):
        with patch.dict(frappe.conf, {flag: True}):
            assert is_scheduler_inactive(verbose=False), flag+' was bypassed'
    ticket=frappe.get_doc('HD Ticket',tickets[primary])
    ticket.subject='workerreplacement '+token
    ticket.save(ignore_permissions=True)
    frappe.db.commit()
    wait_for(lambda: backend.search('workerreplacement')['results'],'committed update')
    assert not backend.search('test.local',title_only=True)['results']
    # A rollback must not expose an uncommitted document through background hooks.
    rolled=make_ticket(subject='rolledbackneedle')
    rolled_name=rolled.name
    frappe.db.rollback()
    assert not frappe.db.exists('HD Ticket',rolled_name)
    time.sleep(2)
    assert not backend.search('rolledbackneedle')['results']
    # Two long workers can race a rebuild against an update/delete.
    rebuild=frappe.enqueue('helpdesk.ticket_search.build_index',queue='long',force=True,refresh=True)
    ticket=frappe.get_doc('HD Ticket',tickets[primary])
    comment=ticket.add_comment('Comment','internalworkerneedle')
    frappe.db.commit()
    wait_for(lambda: backend.search('internalworkerneedle')['results'],'comment indexing')
    frappe.delete_doc('HD Ticket',ticket.name,force=True)
    frappe.db.commit()
    wait_for(lambda: not backend.search('workerreplacement')['results'] and not backend.search('internalworkerneedle')['results'],
             'delete and child removal')
    wait_for(lambda: rebuild.get_status(refresh=True)=='finished','concurrent rebuild')
    # Scheduler recovery must discover the real symlink site and enqueue an actual job.
    backend.drop_index()
    method='helpdesk.ticket_search.build_index_if_not_exists'
    name=frappe.db.get_value('Scheduled Job Type',{'method':method},'name')
    assert name,'Missing registered recovery scheduler hook'
    frappe.db.set_value('Scheduled Job Type',name,'last_execution',add_to_date(now_datetime(),days=-2))
    frappe.db.commit()
    wait_for(backend.index_exists,'symlink site scheduler recovery',seconds=300)
    assert backend.redis.execute_command('GET',other_backend.ready)==other_ready
    connect(other)
    backend=RedisSearchBackend()
    assert backend.search(token)['results'], 'Site A rebuild/delete affected site B'
    connect(primary)
    print('PASS: two real sites sharing Redis DB, colliding ticket IDs/users, commit/rollback, real workers, rebuild/delete and symlink scheduler recovery',flush=True)
