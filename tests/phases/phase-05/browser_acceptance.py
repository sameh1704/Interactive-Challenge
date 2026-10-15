import json,os,re,traceback,time
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
s=json.loads(Path('/tmp/phase5_spec.json').read_text());base=os.environ['P5_BASE'];out=os.environ['P5_OUT'];isolated=os.environ.get('P5_ISOLATED')=='1'
if isolated:assert base=='http://almanar-phase4-validation:8000'
test_prefix=s['prefix']+(' '+os.environ['P5_RUN'] if os.environ.get('P5_RUN') else '')
checks={};errors=[];bad=[];evidence={'base':base,'isolated':isolated};current='setup'
def run(name,fn,critical=True):
 global current
 current=name
 try:
  detail=fn();checks[name]={'status':'PASS'}
  if detail is not None:checks[name]['evidence']=detail
 except Exception as e:
  checks[name]={'status':'FAIL','reason':str(e),'traceback':traceback.format_exc()}
  if critical:raise
with sync_playwright() as p:
 b=p.chromium.launch(channel='chromium',headless=True,args=['--no-sandbox']);contexts=[]
 def page():
  c=b.new_context(viewport={'width':1440,'height':1000});contexts.append(c);pg=c.new_page();pg.set_default_timeout(15000);pg.on('pageerror',lambda e:errors.append(str(e)));pg.on('response',lambda r:bad.append(r.url) if '/static/' in r.url and r.status>=400 else None);return pg
 def visit(pg,path,status=200):
  r=pg.goto(base+path,wait_until='networkidle');assert r.status==status,(path,r.status)
 def switch(pg,lang):
  pg.locator('form.language-switcher button[value='+lang+']').first.click();pg.wait_for_load_state('networkidle');assert pg.locator('html').get_attribute('lang')==lang;assert pg.locator('html').get_attribute('dir')==('rtl' if lang=='ar' else 'ltr')
 def login(pg,role):
  visit(pg,'/accounts/login/');pg.locator('[name=username]').fill(s['users'][role]);pg.locator('[name=password]').fill(s['password']);pg.locator('form').filter(has=pg.locator('[name=username]')).locator('button[type=submit]').click();pg.wait_for_load_state('networkidle');assert '/accounts/login/' not in pg.url
 try:
  teacher=page()
  def signin_languages():
   visit(teacher,'/accounts/login/')
   for lang,label in [('ar','اسم المستخدم'),('en','Username')]:
    switch(teacher,lang);assert label in teacher.locator('label').all_text_contents();teacher.reload(wait_until='networkidle');assert teacher.locator('html').get_attribute('lang')==lang
  run('login_bilingual',signin_languages)
  run('normal_teacher_login',lambda:login(teacher,'teacher'))
  def navigation():
   paths=['/teacher/','/teacher/questions/','/teacher/questions/create/','/teacher/competitions/','/teacher/competitions/create/']
   for path in paths:
    visit(teacher,'/dashboard/');link=teacher.locator('a[href="'+path+'"]').first;assert link.is_visible();link.click();teacher.wait_for_load_state('networkidle');assert teacher.url==base+path
   visit(teacher,'/dashboard/');assert not teacher.locator('a[href="/admin/"]').count();assert not teacher.locator('a[href="/deputy/"]').count()
   assert teacher.request.get(base+'/admin/',max_redirects=0).status==302
   assert teacher.request.get(base+'/deputy/').status==404
   assert teacher.request.post(base+'/teacher/questions/create/',data={'text':'forged'}).status==403
  run('dashboard_click_navigation_and_access',navigation)
  ids=[]
  for kind in ['multiple_choice','true_false','ordering','classification','short_answer']:
   def question(kind=kind):
    visit(teacher,'/dashboard/');teacher.locator('a[href="/teacher/questions/create/"]').first.click();teacher.wait_for_load_state('networkidle');teacher.locator('[name=question_type][value='+kind+']').check();assert teacher.locator('[data-fields-for='+kind+']').is_visible();text=test_prefix+' '+kind;teacher.locator('[name=text]').fill(text);teacher.locator('[name=subject]').select_option(str(s['subject']))
    if kind=='multiple_choice':
     teacher.locator('#multiple-choice-options input[type=text]').nth(0).fill('A');teacher.locator('#multiple-choice-options input[type=text]').nth(1).fill('B');teacher.locator('#multiple-choice-options input[type=radio]').nth(1).check()
    elif kind=='true_false':teacher.locator('[name=builder-correct-answer][value=false]').check()
    elif kind=='ordering':
     teacher.locator('#ordering-items input[type=text]').nth(0).fill('First');teacher.locator('#ordering-items input[type=text]').nth(1).fill('Second');teacher.locator('#ordering-items [data-action=move-ordering-item-up]').nth(1).click()
    elif kind=='classification':
     for i,value in enumerate(['Solid','Liquid']):teacher.locator('#classification-categories input[type=text]').nth(i).fill(value)
     for i,value in enumerate(['Rock','Water']):teacher.locator('#classification-assignments input[type=text]').nth(i).fill(value)
     teacher.locator('#classification-assignments select').nth(0).select_option('Solid');teacher.locator('#classification-assignments select').nth(1).select_option('Liquid')
    else:teacher.locator('#short-answers input[type=text]').first.fill('accepted')
    assert text in teacher.locator('#question-preview').inner_text();teacher.locator('[name=save_option][value=save]').click();teacher.wait_for_load_state('networkidle');assert '/questions/create/' not in teacher.url
    visit(teacher,'/teacher/questions/?q='+kind+'&subject='+str(s['subject']));row=teacher.locator('tr').filter(has_text=text);assert row.count()==1;edit=row.locator('a[href$="/edit/"]').get_attribute('href');qid=int(re.search(r'/questions/(\d+)/',edit).group(1));ids.append(qid)
    visit(teacher,edit);seed=json.loads(teacher.locator('#question-builder-data').text_content());assert seed['text']==text and seed['question_type']==kind
    if kind=='true_false':assert teacher.locator('[name=builder-correct-answer][value=false]').is_checked()
    if kind=='ordering':assert seed['type_config']['correct_order']==['Second','First']
    if kind=='classification':assert seed['type_config']['categories']==['Solid','Liquid']
    teacher.locator('[name=text]').fill(text+' EDITED');teacher.locator('[name=save_option][value=save]').click();teacher.wait_for_load_state('networkidle');visit(teacher,edit);assert teacher.locator('[name=text]').input_value()==text+' EDITED'
    return {'id':qid,'type':kind,'edited_text_reopened':True}
   run('question_'+kind,question)
  evidence['question_ids']=ids
  def save_competition():
   visit(teacher,'/teacher/competitions/create/');teacher.locator('[name=title]').fill(test_prefix+' Acceptance Round');teacher.locator('[name=action][value=next]').click();teacher.wait_for_load_state('networkidle');teacher.locator('[name=subject_id]').select_option(str(s['subject']));teacher.locator('label.classroom-card').filter(has=teacher.locator('[name=classroom_ids][value="'+str(s['rooms']['A1'])+'"]')).click();teacher.locator('[name=action][value=next]').click();teacher.wait_for_load_state('networkidle');teacher.locator('label.question-card').filter(has=teacher.locator('[name=question_ids][value="'+str(ids[1])+'"]')).click();teacher.locator('[name=action][value=next]').click();teacher.wait_for_load_state('networkidle');teacher.locator('[name=action][value=back]').click();teacher.wait_for_load_state('networkidle');teacher.locator('[name=points_'+str(ids[1])+']').fill('17');teacher.locator('[name=action][value=next]').click();teacher.wait_for_load_state('networkidle');teacher.locator('[name=points_per_question]').fill('25');teacher.locator('[name=speed_bonus]').fill('0');teacher.locator('[name=planned_for]').fill('');teacher.locator('[name=action][value=next]').click();teacher.wait_for_load_state('networkidle');assert 'Review' in teacher.locator('body').inner_text();teacher.locator('[name=action][value=save_draft]').click();teacher.wait_for_load_state('networkidle');cid=int(re.search(r'/competitions/(\d+)/',teacher.url).group(1));evidence['competition_id']=cid;visit(teacher,f'/teacher/competitions/{cid}/');assert test_prefix+' Acceptance Round' in teacher.locator('h1').inner_text();assert teacher.locator('[name=planned_for]').input_value()=='';assert teacher.locator('form[action$="/start/"]').count()==1
   return {'id':cid,'unscheduled_draft_reopened':True}
  run('save_draft_reopen',save_competition)
  cid=evidence['competition_id']
  def schedule():
   for when in (['2030-01-01T10:30','2020-01-01T10:30','2030-01-01T10:30'] if isolated else ['2030-01-01T10:30']):
    visit(teacher,f'/teacher/competitions/{cid}/');teacher.locator('[name=planned_for]').fill(when);teacher.locator('form[action$="/plan/"] button[type=submit]').click();teacher.wait_for_load_state('networkidle');teacher.reload(wait_until='networkidle');assert teacher.locator('[name=planned_for]').input_value()==when;assert teacher.locator('form[action$="/start/"]').count()==1
   visit(teacher,'/teacher/competitions/?status=scheduled');assert test_prefix+' Acceptance Round' in teacher.locator('body').inner_text();return {'planned_for_input':'2030-01-01T10:30','past_schedule_did_not_start':isolated,'scheduled_filter_found':True}
  run('schedule_save_reopen_without_start',schedule)
  admin=page();login(admin,'admin');deputy=page();login(deputy,'deputy')
  pages={
   'teacher':(teacher,[('/dashboard/','لوحة التحكم','Dashboard'),('/teacher/','لوحة المعلم','Teacher Dashboard'),('/teacher/questions/','بنك الأسئلة','Question Bank'),('/teacher/questions/create/','إنشاء سؤال','Create Question'),(f'/teacher/questions/{ids[0]}/','تعديل','Edit'),(f'/teacher/questions/{ids[0]}/edit/','حفظ','Save'),('/teacher/competitions/','مسابقاتي','My Competitions'),('/teacher/competitions/create/','مسابقة','Competition'),(f'/teacher/competitions/{cid}/','بدء','Start'),('/teacher/reports/','التقارير','Reports'),('/reports/','التقارير','Reports'),(f"/reports/competitions/{s['rounds']['A']}/",'الفصول المشاركة','Participating classrooms'),(f"/reports/classrooms/{s['rooms']['A1']}/",'النقاط','score'),(f'/reports/questions/{ids[0]}/','إحصاءات السؤال','Question statistics'),(f"/scoring/competitions/{s['rounds']['A']}/leaderboard/",'النتيجة النهائية','Final result')]),
   'administrator':(admin,[('/dashboard/','لوحة التحكم','Dashboard'),('/admin/','إدارة المدرسة','School administration'),('/admin/screens/interactivescreen/','الشاشات','screens')]),
   'deputy':(deputy,[('/deputy/','وكيل المرحلة','Stage Deputy'),('/deputy/results/','النتائج','Results'),(f"/deputy/screens/{s['screens']['A1']}/",'الشاشة','Screen')])}
  for role,(pg,items) in pages.items():
   for lang in ['ar','en']:
    visit(pg,items[0][0]);switch(pg,lang);pg.reload(wait_until='networkidle');assert pg.locator('html').get_attribute('lang')==lang
    for path,ar,en in items:
     def translated(pg=pg,path=path,label=ar if lang=='ar' else en):
      visit(pg,path);assert pg.locator('html').get_attribute('lang')==lang;assert pg.locator('html').get_attribute('dir')==('rtl' if lang=='ar' else 'ltr');text=pg.locator('body').inner_text();assert label.casefold() in text.casefold(),(path,label)
      if lang=='ar' and path=='/admin/screens/interactivescreen/':
       title=pg.locator('h1').inner_text();headers=pg.locator('#result_list thead').inner_text();assert not re.search('interactive screen|Classroom|Status|Primary|Name|Active',title+' '+headers,re.I), 'Untranslated screen administration heading/columns'
      return {'path':path,'language':lang,'direction':'rtl' if lang=='ar' else 'ltr'}
     run('language_'+role+'_'+lang+'_'+path,translated,critical=False)
  visit(teacher,'/dashboard/');switch(teacher,'ar');teacher.screenshot(path=out+'-teacher-ar.png',full_page=True);switch(teacher,'en')
  if isolated:
   events={'A':[],'B':[]};screens=[]
   def monitor(pg,key):
    def websocket(ws):
     def frame(raw):
      try:
       msg=json.loads(raw);events[key].append({k:msg[k] for k in ['type','competition_id','competition_title','state'] if k in msg})
      except (ValueError,TypeError):pass
     ws.on('framereceived',frame);ws.on('close',lambda *args:events[key].append({'type':'socket_closed'}))
    pg.on('websocket',websocket)
   def start_and_delivery():
    for key,screenkey in [('A','A1'),('B','B1')]:
     pg=page();monitor(pg,key);visit(pg,'/screen/?screen_id='+s['screen_credentials'][screenkey]);pg.locator('[data-panel=waiting]').wait_for(state='visible');assert pg.locator('[data-panel]:visible').count()==1;assert not pg.locator('[data-panel=question]').is_visible();screens.append(pg)
    visit(teacher,f'/teacher/competitions/{cid}/');form=teacher.locator('form[action$="/start/"]');ack=form.locator('[name=acknowledge_screens]')
    if ack.count():ack.check()
    form.locator('button[type=submit]').click();teacher.wait_for_load_state('networkidle');assert '/live/competitions/' in teacher.url
    deadline=time.monotonic()+15
    while not any(x.get('competition_id')==cid and x['type']=='state_sync' for x in events['A']) and time.monotonic()<deadline:teacher.wait_for_timeout(200)
    assert any(x['type']=='reconnect_now' for x in events['A']);assert any(x.get('competition_id')==cid and x['type']=='state_sync' for x in events['A']);screens[1].locator('[data-panel=offline]').wait_for(state='visible');assert not any(x.get('competition_id')==cid for x in events['B']);assert not screens[1].locator('[data-panel=question]').is_visible();assert screens[1].locator('[data-panel]:visible').count()==1
    return {'target_screen_events':events['A'],'non_target_screen_events':events['B'],'received_state_sync_after_start':True,'non_target_connection_refused_without_round_data':True}
   run('isolated_manual_start_event_delivery',start_and_delivery)
   def question_delivery():
    teacher.locator('[data-control=start_question]').click();screens[0].locator('[data-panel=question]').wait_for(state='visible');screens[0].locator('[data-role=question-text]').filter(has_text=test_prefix+' true_false EDITED').wait_for(state='visible');assert not screens[1].locator('[data-panel=question]').is_visible();screens[0].context.add_cookies([{'name':'django_language','value':'ar','url':base}]);screens[0].reload(wait_until='networkidle');screens[0].locator('[data-panel=question]').wait_for(state='visible');assert screens[0].locator('html').get_attribute('dir')=='rtl';expect(screens[0].locator('[data-role=connection]')).to_have_text('مباشرة');assert screens[0].locator('#screen-status').inner_text()=='متصل';buttons=screens[0].locator('.screen__option-label').all_text_contents();assert buttons==['صح','خطأ'],buttons;screens[0].locator('.screen__option').nth(1).click();screens[0].locator('[data-role=submit]').click();screens[0].locator('[data-panel=locked]').wait_for(state='visible');assert 'تم إرسال الإجابة' in screens[0].locator('[data-role=locked-text]').inner_text();screens[0].screenshot(path=out+'-screen-ar.png',full_page=True);return {'actual_question_rendered':True,'arabic_dynamic_labels':True,'false_answer_submitted_without_translating_wire_value':True}
   run('isolated_question_delivery_and_arabic_dynamic_ui',question_delivery)
   def repeat_and_finish():
    response=teacher.request.get(base+f'/teacher/competitions/{cid}/');assert response.status==200
    token=teacher.locator('[name=csrfmiddlewaretoken]').first.input_value();again=teacher.request.post(base+f'/teacher/competitions/{cid}/start/',headers={'X-CSRFToken':token,'Referer':base+'/teacher/'},form={'acknowledge_screens':'1'});assert again.status==200;assert 'already been started' in again.text();teacher.locator('[data-control=finish_competition]').click();screens[0].locator('[data-panel=finished]').wait_for(state='visible');teacher.locator('[data-control=start_competition]').wait_for(state='visible');expect(teacher.locator('[data-control=start_competition]')).to_be_disabled();assert 'انتهت' in screens[0].locator('[data-role=final-summary]').inner_text();evidence['isolated_events']=events;return {'repeat_start_refused':True,'start_disabled_after_finish':True,'finish_reached_target_screen':True}
   run('isolated_repeat_start_and_finish',repeat_and_finish)
  else:
   for key in ['isolated_manual_start_event_delivery','isolated_question_delivery_and_arabic_dynamic_ui','isolated_repeat_start_and_finish']:checks[key]={'status':'NOT TESTED','reason':'Production broadcasts deliberately excluded; tested on isolated Redis and temporary screens.'}
 except Exception as exc:
  evidence['stopped_at']=current
  checks['unexpected_failure']={'status':'FAIL','reason':str(exc),'traceback':traceback.format_exc()}
 finally:
  checks['javascript_errors']={'status':'PASS' if not errors else 'FAIL','errors':errors};checks['static_requests']={'status':'PASS' if not bad else 'FAIL','failed_requests':bad};evidence['checks']=checks;Path(out+'.json').write_text(json.dumps(evidence,indent=2,ensure_ascii=False));b.close()
print(json.dumps(evidence,indent=2,ensure_ascii=False))
raise SystemExit(1 if any(x['status']=='FAIL' for x in checks.values()) else 0)
