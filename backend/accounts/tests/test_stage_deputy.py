"""Authorization assertions across the real HTTP and ASGI surfaces."""
from datetime import timedelta
from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from accounts.permissions import is_staff_member
from accounts.stage_scope import competitions_for_deputy
from classrooms.models import SchoolStage, Classroom
from competitions.models import Competition, CompetitionClassroom
from core.tests.factories import create_administrator, create_teacher, create_classroom, create_competition, create_question, create_screen
from live.services import assert_can_control, CompetitionControlError
from scoring.models import CompetitionResult
from screens.models import InteractiveScreen
User = get_user_model()


class StageDeputyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_administrator(username='p4.admin')
        cls.teacher = create_teacher(username='p4.teacher')
        cls.deputy = User.objects.create_user(username='p4.deputy', password='p4-secure-test-password', role='stage_deputy')
        cls.empty = User.objects.create_user(username='p4.empty', role='stage_deputy')
        cls.a = SchoolStage.objects.create(name='Authorized stage', code='p4-a')
        cls.b = SchoolStage.objects.create(name='PRIVATE STAGE', code='p4-b')
        cls.a.deputies.add(cls.deputy)
        cls.room = create_classroom(name='Authorized classroom', stage=cls.a)
        cls.second = create_classroom(name='Second authorized classroom', stage=cls.a)
        cls.out = create_classroom(name='PRIVATE CLASSROOM', stage=cls.b)
        cls.unclassified = create_classroom(name='UNCLASSIFIED')
        # Even an old teacher assignment cannot broaden the deputy's scope.
        cls.out.teachers.add(cls.deputy)
        cls.room.teachers.add(cls.teacher)
        cls.screen = create_screen(name='Authorized screen', classroom=cls.room, last_seen=timezone.now())
        cls.other_screen = create_screen(name='PRIVATE SCREEN', classroom=cls.out)
        cls.round = create_competition(teacher=cls.teacher, title='Authorized competition', classrooms=[cls.room, cls.second], questions=[create_question()])
        cls.other_round = create_competition(teacher=cls.teacher, title='PRIVATE COMPETITION', classrooms=[cls.out], questions=[create_question(text='Private question')])
        cls.mixed = create_competition(teacher=cls.teacher, title='MIXED PRIVATE COMPETITION', classrooms=[cls.room, cls.out], questions=[create_question(text='Mixed')])
        cls.targetless = create_competition(teacher=cls.teacher, title='TARGETLESS', classrooms=[], questions=[])
        CompetitionResult.objects.create(competition=cls.round, standings=[{'classroom_id':cls.room.pk,'classroom_name':'STALE UNSAFE NAME','score':42,'answers_given':1},{'classroom_id':cls.out.pk,'classroom_name':'PRIVATE CLASSROOM','score':900}])
        CompetitionResult.objects.create(competition=cls.other_round, standings=[{'classroom_id':cls.out.pk,'score':999,'answers_given':1}])

    def setUp(self):
        self.client.force_login(self.deputy)

    def test_role_is_not_admin_staff_or_superuser(self):
        self.assertFalse(self.deputy.is_staff)
        self.assertFalse(self.deputy.is_superuser)
        self.assertFalse(is_staff_member(self.deputy))
        self.assertFalse(self.deputy.has_perm('accounts.change_user'))

    def test_old_django_permissions_do_not_elevate_the_deputy(self):
        from django.contrib.auth.models import Permission, Group
        permission=Permission.objects.get(codename='change_user',content_type__app_label='accounts')
        group=Group.objects.create(name='Legacy administration grants');group.permissions.add(permission)
        self.deputy.groups.add(group);self.deputy.user_permissions.add(permission)
        self.assertFalse(self.deputy.has_perm('accounts.change_user'))
        self.assertFalse(self.deputy.has_module_perms('accounts'))
        self.assertFalse(async_to_sync(self.deputy.ahas_perm)('accounts.change_user'))
        self.assertEqual(self.client.get('/admin/accounts/user/').status_code,404)

    def test_classroom_queryset_ignores_old_teacher_assignments(self):
        self.assertSetEqual(set(Classroom.objects.for_user(self.deputy)), {self.room,self.second})

    def test_dashboard_objects_counts_and_credentials(self):
        r=self.client.get('/deputy/')
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.context['classroom_count'],2)
        self.assertEqual(r.context['screen_count'],1)
        self.assertEqual(r.context['online_count'],1)
        self.assertEqual(r.context['competition_count'],1)
        self.assertContains(r,'Authorized classroom')
        for text in ['PRIVATE','TARGETLESS','UNCLASSIFIED',self.screen.screen_id,self.other_screen.screen_id,'STALE UNSAFE NAME']:
            self.assertNotContains(r,text)
        self.assertEqual(list(r.context['results'])[0]['score'],42)

    def test_empty_assignment_is_closed(self):
        self.client.force_login(self.empty)
        r=self.client.get('/deputy/')
        self.assertEqual(r.context['classroom_count'],0)
        self.assertEqual(r.context['competition_count'],0)
        self.assertEqual(r.context['screen_count'],0)
        self.assertContains(r,'No stages assigned')
        self.assertNotContains(r,self.a.name)

    def test_multiple_stages_and_revocation(self):
        self.b.deputies.add(self.deputy)
        self.assertSetEqual(set(Classroom.objects.for_user(self.deputy)),{self.room,self.second,self.out})
        self.assertIn(self.mixed,competitions_for_deputy(self.deputy))
        self.a.deputies.remove(self.deputy)
        self.assertSetEqual(set(Classroom.objects.for_user(self.deputy)),{self.out})
        self.assertNotIn(self.mixed,competitions_for_deputy(self.deputy))

    def test_inactive_stage_is_closed(self):
        self.a.active=False;self.a.save()
        self.assertFalse(Classroom.objects.for_user(self.deputy).exists())

    def test_authorized_details_and_idor(self):
        for kind,inside,outside in [('classrooms',self.room,self.out),('screens',self.screen,self.other_screen),('competitions',self.round,self.other_round)]:
            with self.subTest(kind=kind):
                r=self.client.get(f'/deputy/{kind}/{inside.pk}/')
                self.assertEqual(r.status_code,200)
                self.assertNotContains(r,'PRIVATE')
                self.assertEqual(self.client.get(f'/deputy/{kind}/{outside.pk}/').status_code,404)
        self.assertEqual(self.client.get(f'/deputy/competitions/{self.mixed.pk}/').status_code,404)

    def test_forged_stage_filters(self):
        for stage in [self.b.pk,999999,'oops','1 OR 1=1','²','9'*100]:
            with self.subTest(stage=stage):
                self.assertEqual(self.client.get('/deputy/',{'stage':stage}).status_code,404)
                self.assertEqual(self.client.get('/deputy/results/export.csv',{'stage':stage}).status_code,404)

    def test_scoped_export_and_filter(self):
        r=self.client.get('/deputy/results/export.csv',{'stage':self.a.pk})
        self.assertEqual(r.status_code,200)
        self.assertIn('42',r.content.decode())
        self.assertNotIn('PRIVATE',r.content.decode())
        self.assertNotIn('STALE',r.content.decode())
        self.assertNotIn('999',r.content.decode())

    def test_search_and_pagination_remain_scoped(self):
        for n in range(28):create_classroom(name=f'Visible {n}',stage=self.a)
        r=self.client.get('/deputy/',{'rooms_page':2,'q':'Visible','stage':self.a.pk})
        self.assertEqual(r.context['classroom_count'],28)
        self.assertEqual(len(r.context['rooms']),3)
        self.assertNotContains(r,'PRIVATE')
        self.assertContains(r,'rooms_page=1')
        self.assertEqual(self.client.get('/deputy/',{'q':'PRIVATE'}).context['classroom_count'],0)

    def test_old_global_and_mutating_surfaces_are_denied(self):
        paths=['/admin/','/admin/accounts/user/add/','/admin/screens/interactivescreen/add/',
            '/teacher/questions/','/teacher/questions/create/','/teacher/competitions/create/',
            f'/teacher/competitions/{self.round.pk}/start/',f'/live/competitions/{self.round.pk}/',
            '/reports/','/reports/questions/1/',f'/reports/classrooms/{self.out.pk}/',
            '/tournaments/','/scoring/','/screen/','/screen/heartbeat/']
        for p in paths:
            for method in [self.client.get,self.client.post]:
                with self.subTest(path=p,method=method.__name__):
                    self.assertEqual(method(p,{'stage':self.b.pk,'role':'administrator'}).status_code,404)
        self.deputy.refresh_from_db();self.assertEqual(self.deputy.role,'stage_deputy')
        self.round.refresh_from_db();self.assertIsNone(self.round.started_at)

    def test_monitoring_posts_cannot_mutate(self):
        for p in ['/deputy/','/deputy/results/',f'/deputy/competitions/{self.round.pk}/','/deputy/results/export.csv']:
            self.assertEqual(self.client.post(p,{'role':'administrator','stage':self.b.pk}).status_code,405)

    def test_owner_cannot_control_after_deputy_role_change(self):
        self.round.teacher=self.deputy;self.round.save()
        with self.assertRaises(CompetitionControlError):assert_can_control(self.round,self.deputy)

    def test_former_tournament_owner_cannot_manage_as_deputy(self):
        from core.tests.factories import create_tournament
        from tournaments.services import assert_can_manage, TournamentError
        from tournaments.models import Tournament
        tournament=create_tournament(created_by=self.deputy)
        with self.assertRaises(TournamentError):assert_can_manage(tournament,self.deputy)
        self.assertFalse(Tournament.objects.for_user(self.deputy).exists())

    def test_dashboard_redirect_and_language_theme(self):
        self.assertRedirects(self.client.get('/dashboard/'),'/deputy/')
        for lang,direction,label in [('ar','rtl','وكيل المرحلة'),('en','ltr','Stage Deputy')]:
            self.client.post('/i18n/setlang/',{'language':lang,'next':'/deputy/'})
            r=self.client.get('/deputy/')
            self.assertContains(r,f'lang="{lang}"')
            self.assertContains(r,f'dir="{direction}"')
            self.assertContains(r,label)
        r=self.client.post('/theme/',{'theme':'dark','next':'/deputy/'})
        self.assertEqual(r.status_code,302)
        self.assertContains(self.client.get('/deputy/'),'data-theme="dark"')

    def test_teacher_and_admin_legacy_access(self):
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.get('/teacher/questions/').status_code,200)
        self.assertSetEqual(set(Classroom.objects.for_user(self.teacher)),{self.room})
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/admin/').status_code,200)
        self.assertIn(self.out,Classroom.objects.for_user(self.admin))

    def test_database_rejects_invalid_roles_and_deputy_superuser(self):
        for extra in [dict(role='made-up'),dict(role='stage_deputy',is_superuser=True)]:
            with self.subTest(extra=extra), self.assertRaises(IntegrityError), transaction.atomic():
                User.objects.create(username='invalid',**extra)

    def test_admin_assigns_and_revokes_scopes_through_actual_forms(self):
        self.client.force_login(self.admin)
        r=self.client.post('/admin/accounts/user/add/',{'username':'p4.created','role':'stage_deputy',
            'password1':'phase4-long-test-password','password2':'phase4-long-test-password','deputy_stages':[self.a.pk,self.b.pk]})
        self.assertEqual(r.status_code,302)
        u=User.objects.get(username='p4.created');self.assertSetEqual(set(u.deputy_stages.all()),{self.a,self.b})
        r=self.client.post(f'/admin/accounts/user/{u.pk}/change/',{'username':u.username,'role':'stage_deputy','is_active':'on','deputy_stages':[self.b.pk]})
        self.assertEqual(r.status_code,302);self.assertSetEqual(set(u.deputy_stages.all()),{self.b})
        self.client.post(f'/admin/accounts/user/{u.pk}/change/',{'username':u.username,'role':'stage_deputy','is_active':'on'})
        self.assertFalse(u.deputy_stages.exists())

    def test_admin_classification_workflow_preserves_teacher_assignments(self):
        self.client.force_login(self.admin)
        response=self.client.post('/admin/classrooms/schoolstage/add/',{
            'name':'New explicit school stage','code':'explicit-stage','active':'on','deputies':[self.deputy.pk]})
        self.assertEqual(response.status_code,302)
        stage=SchoolStage.objects.get(code='explicit-stage')
        response=self.client.post(f'/admin/classrooms/classroom/{self.room.pk}/change/',{
            'name':self.room.name,'stage':stage.pk,'grade':self.room.grade,'teachers':[self.teacher.pk],'active':'on'})
        self.assertEqual(response.status_code,302)
        self.room.refresh_from_db();self.assertEqual(self.room.stage,stage)
        self.assertSetEqual(set(self.room.teachers.all()),{self.teacher})
        response=self.client.post('/admin/classrooms/schoolstage/add/',{
            'name':'Forged deputy','code':'forged-stage','active':'on','deputies':[self.teacher.pk]})
        self.assertEqual(response.status_code,200)
        self.assertFalse(SchoolStage.objects.filter(code='forged-stage').exists())

    def test_admin_forms_reject_nonexistent_scope_and_escalation(self):
        self.client.force_login(self.admin)
        for fields in [dict(role='stage_deputy',deputy_stages=[999999]),dict(role='teacher',deputy_stages=[self.a.pk]),dict(role='stage_deputy',is_superuser='on')]:
            with self.subTest(fields=fields):
                r=self.client.post(f'/admin/accounts/user/{self.deputy.pk}/change/',{'username':self.deputy.username,'is_active':'on',**fields})
                self.assertEqual(r.status_code,200)
                self.deputy.refresh_from_db();self.assertEqual(self.deputy.role,'stage_deputy');self.assertFalse(self.deputy.is_superuser)


@override_settings(CHANNEL_LAYERS={'default':{'BACKEND':'channels.layers.InMemoryChannelLayer'}})
class DeputySocketTests(TransactionTestCase):
    def test_deputy_cannot_use_control_leaderboard_or_screen_sockets(self):
        from config.asgi import application
        from live.tests.support import build_teacher_scope, origin_headers
        deputy=User.objects.create_user(username='socket.deputy',role='stage_deputy')
        room=create_classroom(name='Socket room')
        screen=create_screen(classroom=room)
        competition=create_competition(teacher=deputy,classrooms=[room],questions=[create_question()])
        async def run():
            scope=await build_teacher_scope(deputy,competition.pk)
            for path in [f'/ws/live/teacher/{competition.pk}/',f'/ws/live/leaderboard/{competition.pk}/',f'/ws/live/screen/?screen_id={screen.screen_id}']:
                ws=WebsocketCommunicator(application,path,headers=scope['headers'])
                connected,_=await ws.connect(timeout=15)
                self.assertFalse(connected,path)
                await ws.disconnect()
        async_to_sync(run)()

    def test_open_teacher_socket_closes_after_role_is_changed(self):
        from config.asgi import application
        from live.tests.support import build_teacher_scope
        from channels.db import database_sync_to_async
        teacher=create_teacher(username='p4.converted')
        competition=create_competition(teacher=teacher,classrooms=[create_classroom(name='Revoked room')],questions=[create_question()])
        async def run():
            scope=await build_teacher_scope(teacher,competition.pk)
            ws=WebsocketCommunicator(application,f'/ws/live/teacher/{competition.pk}/',headers=scope['headers'])
            ok,_=await ws.connect(timeout=15);self.assertTrue(ok)
            await ws.receive_json_from(timeout=10);await ws.receive_json_from(timeout=10)
            await database_sync_to_async(User.objects.filter(pk=teacher.pk).update)(role='stage_deputy')
            await ws.send_json_to({'type':'start_competition'})
            event=await ws.receive_output(timeout=10)
            self.assertEqual(event['type'],'websocket.close')
            await ws.disconnect()
        async_to_sync(run)()
        competition.refresh_from_db();self.assertIsNone(competition.started_at)

    def test_open_device_socket_closes_after_role_is_changed(self):
        from config.asgi import application
        from live.tests.support import build_teacher_scope
        from channels.db import database_sync_to_async
        teacher=create_teacher(username='p4.device.converted')
        room=create_classroom(name='Device revoked room')
        screen=create_screen(classroom=room)
        competition=create_competition(teacher=teacher,classrooms=[room],questions=[create_question()])
        async def run():
            scope=await build_teacher_scope(teacher,competition.pk)
            ws=WebsocketCommunicator(application,f'/ws/live/screen/?screen_id={screen.screen_id}',headers=scope['headers'])
            ok,_=await ws.connect(timeout=15);self.assertTrue(ok)
            waiting=await ws.receive_json_from(timeout=10)
            self.assertEqual(waiting['type'],'waiting')
            await database_sync_to_async(User.objects.filter(pk=teacher.pk).update)(role='stage_deputy')
            await ws.send_json_to({'type':'ping'})
            event=await ws.receive_output(timeout=10)
            self.assertEqual(event['type'],'websocket.close')
            await ws.disconnect()
        async_to_sync(run)()
