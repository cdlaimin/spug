# Copyright: (c) OpenSpug Organization. https://github.com/openspug/spug
# Copyright: (c) <spug.dev@gmail.com>
# Released under the AGPL-3.0 License.
from django.views.generic import View
from django.db.models import F
from django.conf import settings
from django.http.response import HttpResponseBadRequest
from libs import json_response, JsonParser, Argument, human_datetime, human_time, auth, AttrDict
from apps.deploy.models import DeployRequest
from apps.app.models import Deploy, DeployExtend2
from apps.repository.models import Repository
from apps.deploy.utils import dispatch, Helper
from apps.host.models import Host
from collections import defaultdict
from threading import Thread
from datetime import datetime
import subprocess
import json
import os


class RequestView(View):
    @auth('deploy.request.view')
    def get(self, request):
        data, query, counter = [], {}, {}
        if not request.user.is_supper:
            perms = request.user.deploy_perms
            query['deploy__app_id__in'] = perms['apps']
            query['deploy__env_id__in'] = perms['envs']
        for item in DeployRequest.objects.filter(**query).annotate(
                env_id=F('deploy__env_id'),
                env_name=F('deploy__env__name'),
                app_id=F('deploy__app_id'),
                app_name=F('deploy__app__name'),
                app_host_ids=F('deploy__host_ids'),
                app_extend=F('deploy__extend'),
                rep_extra=F('repository__extra'),
                do_by_user=F('do_by__nickname'),
                approve_by_user=F('approve_by__nickname'),
                created_by_user=F('created_by__nickname')):
            tmp = item.to_dict()
            tmp['env_id'] = item.env_id
            tmp['env_name'] = item.env_name
            tmp['app_id'] = item.app_id
            tmp['app_name'] = item.app_name
            tmp['app_extend'] = item.app_extend
            tmp['host_ids'] = json.loads(item.host_ids)
            tmp['deploy_status'] = json.loads(item.deploy_status)
            tmp['extra'] = json.loads(item.extra) if item.extra else None
            tmp['rep_extra'] = json.loads(item.rep_extra) if item.rep_extra else None
            tmp['app_host_ids'] = json.loads(item.app_host_ids)
            tmp['status_alias'] = item.get_status_display()
            tmp['created_by_user'] = item.created_by_user
            tmp['approve_by_user'] = item.approve_by_user
            tmp['do_by_user'] = item.do_by_user
            if item.app_extend == '1':
                tmp['visible_rollback'] = item.deploy_id not in counter
                counter[item.deploy_id] = True
            data.append(tmp)
        return json_response(data)

    @auth('deploy.request.del')
    def delete(self, request):
        form, error = JsonParser(
            Argument('id', type=int, required=False),
            Argument('mode', filter=lambda x: x in ('count', 'expire', 'deploy'), required=False, help='参数错误'),
            Argument('value', required=False),
        ).parse(request.GET)
        if error is None:
            if form.id:
                deploy = DeployRequest.objects.filter(pk=form.id).first()
                if not deploy or deploy.status not in ('0', '1', '-1'):
                    return json_response(error='未找到指定发布申请或当前状态不允许删除')
                deploy.delete()
                return json_response()

            count = 0
            if form.mode == 'count':
                if not str(form.value).isdigit() or int(form.value) < 1:
                    return json_response(error='请输入正确的保留数量')
                counter, form.value = defaultdict(int), int(form.value)
                for item in DeployRequest.objects.all():
                    counter[item.deploy_id] += 1
                    if counter[item.deploy_id] > form.value:
                        count += 1
                        item.delete()
            elif form.mode == 'expire':
                for item in DeployRequest.objects.filter(created_at__lt=form.value):
                    count += 1
                    item.delete()
            elif form.mode == 'deploy':
                app_id, env_id = str(form.value).split(',')
                for item in DeployRequest.objects.filter(deploy__app_id=app_id, deploy__env_id=env_id):
                    count += 1
                    item.delete()
            return json_response(count)
        return json_response(error=error)


class RequestDetailView(View):
    @auth('deploy.request.view')
    def get(self, request, r_id):
        req = DeployRequest.objects.filter(pk=r_id).first()
        if not req:
            return json_response(error='未找到指定发布申请')
        response = AttrDict(status=req.status)
        hosts = Host.objects.filter(id__in=json.loads(req.host_ids))
        outputs = {x.id: {'id': x.id, 'title': x.name, 'data': ''} for x in hosts}
        outputs['local'] = {'id': 'local', 'data': ''}
        if req.deploy.extend == '2':
            s_actions = json.loads(req.deploy.extend_obj.server_actions)
            h_actions = json.loads(req.deploy.extend_obj.host_actions)
            if not s_actions:
                outputs.pop('local')
            if not h_actions:
                outputs = {'local': outputs['local']}
        elif not req.is_quick_deploy:
            outputs.pop('local')

        response.index = Helper.fill_outputs(outputs, req.deploy_key)
        response.token = req.deploy_key
        response.outputs = outputs
        return json_response(response)

    @auth('deploy.request.do')
    def post(self, request, r_id):
        form, error = JsonParser(
            Argument('mode', filter=lambda x: x in ('fail', 'gray', 'all'), help='参数错误'),
            Argument('host_ids', type=list, required=False)
        ).parse(request.body)
        if error is None:
            query, is_fail_mode = {'pk': r_id}, form.mode == 'fail'
            if not request.user.is_supper:
                perms = request.user.deploy_perms
                query['deploy__app_id__in'] = perms['apps']
                query['deploy__env_id__in'] = perms['envs']
            req = DeployRequest.objects.filter(**query).first()
            if not req:
                return json_response(error='未找到指定发布申请')
            if req.status not in ('1', '-3', '4'):
                return json_response(error='该申请单当前状态还不能执行发布')

            deploy_status = json.loads(req.deploy_status)
            if form.mode == 'gray':
                if not form.host_ids:
                    return json_response(error='请选择灰度发布的主机')
                host_ids = form.host_ids
            elif form.mode == 'fail':
                host_ids = [int(k) for k, v in deploy_status.items() if v != '2' and k != 'local']
            else:
                host_ids = json.loads(req.host_ids)

            with_local = False
            hosts = Host.objects.filter(id__in=host_ids)
            message = Helper.term_message('等待调度...        ')
            outputs = {x.id: {'id': x.id, 'title': x.name, 'data': message} for x in hosts}
            if req.deploy.extend == '1':
                if req.repository_id:
                    if req.is_quick_deploy:
                        outputs['local'] = {
                            'id': 'local',
                            'status': 'success',
                            'data': Helper.term_message('已构建完成忽略执行', 'warn')
                        }
                else:
                    with_local = True
                    outputs['local'] = {'id': 'local', 'data': Helper.term_message('等待初始化...        ')}
            elif req.deploy.extend == '2':
                s_actions = json.loads(req.deploy.extend_obj.server_actions)
                h_actions = json.loads(req.deploy.extend_obj.host_actions)
                if s_actions:
                    if deploy_status.get('local') == '2':
                        outputs['local'] = {
                            'id': 'local',
                            'status': 'success',
                            'data': Helper.term_message('已完成本地动作忽略执行', 'warn')
                        }
                    else:
                        with_local = True
                        outputs['local'] = {'id': 'local', 'data': Helper.term_message('等待初始化...        ')}
                if not h_actions:
                    outputs = {'local': outputs['local']}
            else:
                raise NotImplementedError

            req.status = '2'
            req.do_at = human_datetime()
            req.do_by = request.user
            req.save()
            Thread(target=dispatch, args=(req, host_ids, with_local)).start()
            return json_response({'outputs': outputs, 'token': req.deploy_key})
        return json_response(error=error)

    @auth('deploy.request.approve')
    def patch(self, request, r_id):
        form, error = JsonParser(
            Argument('reason', required=False),
            Argument('is_pass', type=bool, help='参数错误')
        ).parse(request.body)
        if error is None:
            req = DeployRequest.objects.filter(pk=r_id).first()
            if not req:
                return json_response(error='未找到指定申请')
            if not form.is_pass and not form.reason:
                return json_response(error='请输入驳回原因')
            if req.status != '0':
                return json_response(error='该申请当前状态不允许审核')
            req.approve_at = human_datetime()
            req.approve_by = request.user
            req.status = '1' if form.is_pass else '-1'
            req.reason = form.reason
            req.save()
            Thread(target=Helper.send_deploy_notify, args=(req, 'approve_rst')).start()
        return json_response(error=error)


@auth('deploy.request.add|deploy.request.edit')
def post_request_ext1(request):
    form, error = JsonParser(
        Argument('id', type=int, required=False),
        Argument('deploy_id', type=int, help='参数错误'),
        Argument('name', help='请输入申请标题'),
        Argument('extra', type=list, help='请选择发布版本'),
        Argument('host_ids', type=list, filter=lambda x: len(x), help='请选择要部署的主机'),
        Argument('type', default='1'),
        Argument('plan', required=False),
        Argument('desc', required=False),
    ).parse(request.body)
    if error is None:
        deploy = Deploy.objects.get(pk=form.deploy_id)
        form.spug_version = Repository.make_spug_version(deploy.id)
        if form.extra[0] == 'tag':
            if not form.extra[1]:
                return json_response(error='请选择要发布的版本')
            form.version = form.extra[1]
        elif form.extra[0] == 'branch':
            if not form.extra[2]:
                return json_response(error='请选择要发布的分支及Commit ID')
            form.version = f'{form.extra[1]}#{form.extra[2][:6]}'
        elif form.extra[0] == 'repository':
            if not form.extra[1]:
                return json_response(error='请选择要发布的版本')
            repository = Repository.objects.get(pk=form.extra[1])
            form.repository_id = repository.id
            form.version = repository.version
            form.spug_version = repository.spug_version
            form.extra = ['repository'] + json.loads(repository.extra)
        else:
            return json_response(error='参数错误')

        form.extra = json.dumps(form.extra)
        form.status = '0' if deploy.is_audit else '1'
        form.host_ids = json.dumps(sorted(form.host_ids))
        if form.id:
            req = DeployRequest.objects.get(pk=form.id)
            is_required_notify = deploy.is_audit and req.status == '-1'
            DeployRequest.objects.filter(pk=form.id).update(created_by=request.user, reason=None, **form)
        else:
            req = DeployRequest.objects.create(created_by=request.user, **form)
            is_required_notify = deploy.is_audit
        if is_required_notify:
            Thread(target=Helper.send_deploy_notify, args=(req, 'approve_req')).start()
    return json_response(error=error)


@auth('deploy.request.do')
def post_request_ext1_rollback(request):
    form, error = JsonParser(
        Argument('request_id', type=int, help='请选择要回滚的版本'),
        Argument('name', help='请输入申请标题'),
        Argument('host_ids', type=list, filter=lambda x: len(x), help='请选择要部署的主机'),
        Argument('desc', required=False),
    ).parse(request.body)
    if error is None:
        req = DeployRequest.objects.get(pk=form.pop('request_id'))
        requests = DeployRequest.objects.filter(deploy=req.deploy, status__in=('3', '-3'))
        versions = list({x.spug_version: 1 for x in requests}.keys())
        if req.spug_version not in versions[:req.deploy.extend_obj.versions + 1]:
            return json_response(
                error='选择的版本超出了发布配置中设置的版本数量，无法快速回滚，可通过新建发布申请选择构建仓库里的该版本再次发布。')

        form.status = '0' if req.deploy.is_audit else '1'
        form.host_ids = json.dumps(sorted(form.host_ids))
        new_req = DeployRequest.objects.create(
            deploy_id=req.deploy_id,
            repository_id=req.repository_id,
            type='2',
            extra=req.extra,
            version=req.version,
            spug_version=req.spug_version,
            created_by=request.user,
            **form
        )
        if req.deploy.is_audit:
            Thread(target=Helper.send_deploy_notify, args=(new_req, 'approve_req')).start()
    return json_response(error=error)


@auth('deploy.request.add|deploy.request.edit')
def post_request_ext2(request):
    form, error = JsonParser(
        Argument('id', type=int, required=False),
        Argument('deploy_id', type=int, help='缺少必要参数'),
        Argument('name', help='请输申请标题'),
        Argument('host_ids', type=list, filter=lambda x: len(x), help='请选择要部署的主机'),
        Argument('extra', type=dict, required=False),
        Argument('version', default=''),
        Argument('type', default='1'),
        Argument('plan', required=False),
        Argument('desc', required=False),
    ).parse(request.body)
    if error is None:
        deploy = Deploy.objects.filter(pk=form.deploy_id).first()
        if not deploy:
            return json_response(error='未找到该发布配置')
        extra = form.pop('extra')
        if DeployExtend2.objects.filter(deploy=deploy, host_actions__contains='"src_mode": "1"').exists():
            if not extra:
                return json_response(error='该应用的发布配置中使用了数据传输动作且设置为发布时上传，请上传要传输的数据')
            form.spug_version = extra['path']
            form.extra = json.dumps(extra)
        else:
            form.spug_version = Repository.make_spug_version(deploy.id)
        form.name = form.name.replace("'", '')
        form.status = '0' if deploy.is_audit else '1'
        form.host_ids = json.dumps(form.host_ids)
        if form.id:
            req = DeployRequest.objects.get(pk=form.id)
            is_required_notify = deploy.is_audit and req.status == '-1'
            form.update(created_by=request.user, reason=None)
            req.update_by_dict(form)
        else:
            req = DeployRequest.objects.create(created_by=request.user, **form)
            is_required_notify = deploy.is_audit
        if is_required_notify:
            Thread(target=Helper.send_deploy_notify, args=(req, 'approve_req')).start()
    return json_response(error=error)


@auth('deploy.request.view')
def get_request_info(request):
    form, error = JsonParser(
        Argument('id', type=int, help='参数错误')
    ).parse(request.GET)
    if error is None:
        req = DeployRequest.objects.get(pk=form.id)
        response = req.to_dict(selects=('status', 'reason'))
        response['deploy_status'] = json.loads(req.deploy_status)
        response['status_alias'] = req.get_status_display()
        return json_response(response)
    return json_response(error=error)


@auth('deploy.request.add')
def do_upload(request):
    repos_dir = settings.REPOS_DIR
    file = request.FILES['file']
    deploy_id = request.POST.get('deploy_id')
    if file and deploy_id:
        dir_name = os.path.join(repos_dir, deploy_id)
        file_name = datetime.now().strftime("%Y%m%d%H%M%S")
        command = f'mkdir -p {dir_name} && cd {dir_name} && ls | sort  -rn | tail -n +11 | xargs rm -rf'
        code, outputs = subprocess.getstatusoutput(command)
        if code != 0:
            return json_response(error=outputs)
        with open(os.path.join(dir_name, file_name), 'wb') as f:
            for chunk in file.chunks():
                f.write(chunk)
        return json_response(file_name)
    else:
        return HttpResponseBadRequest()
