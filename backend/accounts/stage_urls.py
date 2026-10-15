from django.urls import path
from accounts.stage_views import DeputyView
app_name = 'deputy'
urlpatterns = [
    path('', DeputyView.as_view(), name='dashboard'),
    path('classrooms/<int:pk>/', DeputyView.as_view(), {'kind':'classrooms'}, name='classroom'),
    path('screens/<int:pk>/', DeputyView.as_view(), {'kind':'screens'}, name='screen'),
    path('competitions/<int:pk>/', DeputyView.as_view(), {'kind':'competitions'}, name='competition'),
    path('results/', DeputyView.as_view(), {'kind':'results'}, name='results'),
    path('results/export.csv', DeputyView.as_view(), {'kind':'export'}, name='export'),
]
