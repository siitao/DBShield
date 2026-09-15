# -*- coding: UTF-8 -*-

from sql.models import Users, Instance, ResourceGroup


def user_groups(user):
    """
    获取用户关联资源组列表
    :param user:
    :return:
    """
    if user.is_superuser:
        group_list = [group for group in ResourceGroup.objects.filter(is_deleted=0)]
    else:
        group_list = [
            group
            for group in Users.objects.get(id=user.id).resource_group.filter(
                is_deleted=0
            )
        ]
    return group_list


def user_instances(user, type=None, db_type=None, tag_codes=None):
    """
    获取用户实例列表（通过资源组间接关联）
    :param user:
    :param type: 实例类型 all：全部，master主库，salve从库
    :param db_type: 数据库类型, ['mysql','mssql']
    :param tag_codes: 标签code列表, ['can_write', 'can_read']
    :return:
    """
    # 拥有所有实例权限的用户
    if user.has_perm("sql.query_all_instances"):
        instances = Instance.objects.all()
    else:
        # 先获取用户关联的资源组
        resource_groups = ResourceGroup.objects.filter(users=user, is_deleted=0)
        # 再获取实例
        instances = Instance.objects.filter(resource_group__in=resource_groups)
    # 过滤type
    if type:
        instances = instances.filter(type=type)

    # 过滤db_type
    if db_type:
        instances = instances.filter(db_type__in=db_type)

    # 过滤tag
    if tag_codes:
        for tag_code in tag_codes:
            instances = instances.filter(
                instance_tag__tag_code=tag_code, instance_tag__active=True
            )
    return instances.distinct()


def get_current_reviewers(review_nodes, resource_group_name):
    """
    当前审核人：审核流当前节点权限组内、属于指定资源组的活跃用户。

    批量预取实现——旧写法对每个节点、每个候选用户各发一条查询
    （node.group.user_set + user_groups(user)），审批组大时工单详情页
    一次请求可产生几十至上百条 SQL；这里一次查出节点组用户并
    prefetch 资源组后过滤。

    :param review_nodes: ReviewInfo.nodes（AuditV2.get_review_info() 的结果）
    :param resource_group_name: 工单/申请所属资源组名
    :return: [{"username": ..., "display": ...}]
    """
    group_ids = [n.group.id for n in review_nodes if n.is_current_node and n.group]
    if not group_ids:
        return []
    users = (
        Users.objects.filter(groups__in=group_ids, is_active=1)
        .prefetch_related("resource_group")
        .distinct()
    )
    reviewers = []
    for user in users:
        group_names = [
            g.group_name for g in user.resource_group.all() if not g.is_deleted
        ]
        if resource_group_name in group_names:
            reviewers.append(
                {"username": user.username, "display": user.display or user.username}
            )
    return reviewers


def auth_group_users(auth_group_names, group_id):
    """
    获取资源组内关联指定权限组的用户
    :param auth_group_names: 权限组名称list
    :param group_id: 资源组ID
    :return:
    """
    # 获取资源组关联的用户
    users = ResourceGroup.objects.get(group_id=group_id).users_set.all()
    # 过滤在该权限组中的用户
    users = users.filter(groups__name__in=auth_group_names, is_active=1)
    return users
