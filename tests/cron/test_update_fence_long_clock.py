"""Real scheduler ticks through a real update marker with virtual time: 6 or 40 min; 120 s one-shot grace."""
from datetime import timedelta
import pytest
from tests.cron.test_xcron_fence_delays_never_changes import _fence, _due, J, S, scheduler_authority
from tests.cron.test_xcron_fence_delays_never_changes import cron_home  # pytest fixture

@pytest.mark.parametrize('minutes',[6,40])
def test_long_update(cron_home,monkeypatch,minutes):
    from cron import dispatch_hold as D
    from cron.occurrences import get_catch_up_occurrence_count
    t0=J._hermes_now().replace(microsecond=0); now=[t0];submitted=[]
    monkeypatch.setattr(J,'_hermes_now',lambda:now[0]); monkeypatch.setattr(D,'time',type('Clock',(),{'time':lambda:now[0].timestamp()}))
    monkeypatch.setattr(scheduler_authority,'run_canonical_job',lambda job,**kw:(submitted.append(job['id']) or True,'# ok','ok',None))
    one=J.create_job(prompt='local only',schedule=(t0+timedelta(seconds=60)).isoformat(),name='once',deliver='local')['id']; catch=J.create_job(prompt='local only',schedule='every 1h',name='catchup',repeat=3,deliver='local')['id'];_due(catch,timedelta(hours=3))
    marker=_fence(cron_home)
    for n in range(minutes+1):
        now[0]=t0+timedelta(minutes=n);S.tick(verbose=False,sync=True)
    data={j['id']:j for j in J.load_jobs()}; assert not submitted and get_catch_up_occurrence_count()==0;assert data[one]['repeat']['completed']==data[catch]['repeat']['completed']==0
    marker.unlink();now[0]+=timedelta(seconds=1);S.tick(verbose=False,sync=True); S.tick(verbose=False,sync=True)
    assert submitted.count(one)==1,'the proven update made a never-fired one-shot expire'; assert submitted.count(catch)==1;assert get_catch_up_occurrence_count()==1
    data={j['id']:j for j in J.load_jobs()};assert data[catch]['repeat']['completed']==1 and data[catch]['failure_streak']==0
