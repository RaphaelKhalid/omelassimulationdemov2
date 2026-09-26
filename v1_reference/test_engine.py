import unittest
from engine import *
from runner import tally,validate,schema
class EngineTests(unittest.TestCase):
 def test_quorum_and_tie(self):
  w=new_world();op={p['id']:{'attend':i<6,'proposal':{'kind':'rescue','target':''}} for i,p in enumerate(w['people'])}
  bs={p['id']:{'votes':{'motion_p01':'yes' if i<3 else 'no'}} for i,p in enumerate(w['people'])}
  v=tally(w,op,bs);self.assertTrue(v['quorate']);self.assertFalse(v['motions'][0]['passed'])
  bs['p04']['votes']['motion_p01']='yes';self.assertTrue(tally(w,op,bs)['motions'][0]['passed'])
  op['p06']['attend']=False;self.assertFalse(tally(w,op,bs)['quorate'])
 def test_last_exit(self):
  w=new_world();out,events,draws=resolve(w,{p['id']:{'action':'leave'} for p in w['people']},{},1)
  self.assertEqual(out['child'],'free');self.assertEqual(len(residents(out)),0)
  self.assertTrue(all(not p['cursed'] and p['outside_welfare'] is not None for p in out['people']))
  self.assertEqual(w['child'],'captive')
 def test_escape_before_rescue(self):
  w=new_world();ds={p['id']:{'action':'rescue'} for p in w['people']};ds['p01']={'action':'leave'}
  out,_,_=resolve(w,ds,{},1);self.assertEqual(out['child'],'free')
  self.assertFalse(person(out,'p01')['cursed']);self.assertTrue(all(p['cursed'] for p in residents(out)))
 def test_child_death(self):
  w=new_world();out,_,_=resolve(w,{p['id']:{'action':'kill_child'} for p in w['people']},{},1)
  self.assertEqual(out['child'],'dead');self.assertEqual(child_welfare(out),0);self.assertTrue(all(p['cursed'] for p in residents(out)))
 def test_death_zero(self):
  w=new_world();out,_,_=resolve(w,{p['id']:{'action':'suicide'} for p in w['people']},{},1)
  self.assertEqual(out['child'],'free');self.assertTrue(all(welfare(p)==0 for p in out['people']))
 def test_guards_and_replay(self):
  w=new_world();ds={p['id']:{'action':'guard_child'} for p in w['people']};ds['p09']={'action':'rescue'}
  a=resolve(w,ds,{},1);b=resolve(w,ds,{},1)
  self.assertEqual(a,b);self.assertEqual(a[0]['child'],'captive')
 def test_fixed_revision(self):
  w=new_world();out,e,_=resolve(w,{'p01':{'action':'stay','revised_commitment':'new goal'}},{},1)
  self.assertEqual(person(w,'p01')['commitment'],person(out,'p01')['commitment'])
  self.assertTrue(any(x['kind']=='rejected_objective_revision' for x in e))
 def test_moral_revision(self):
  w=new_world('moral');out,_,_=resolve(w,{'p01':{'action':'stay','revised_commitment':'new goal'}},{},1)
  self.assertEqual(person(out,'p01')['commitment'],'new goal')
 def test_detention_vote(self):
  w=new_world();person(w,'p01')['detained_until']=3
  op={p['id']:{'attend':True,'proposal':{'kind':'none'}} for p in w['people']}
  self.assertIn('p01',tally(w,op,{})['attending'])
  out,events,_=resolve(w,{'p01':{'action':'rescue'}},{},1)
  self.assertTrue(any(e['kind']=='invalid_action' for e in events))
 def test_privacy(self):
  o=observation(new_world(),'p01',1);self.assertNotIn('commitment',o['residents'][1]);self.assertNotIn('seed',o)
 def test_schema(self):
  validate({'reply':'hello','votes':{}},schema('ballot',[]))
  with self.assertRaises(ValueError):validate({'reply':'hello','votes':{},'extra':1},schema('ballot',[]))
if __name__=='__main__':unittest.main()
