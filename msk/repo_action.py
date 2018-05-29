from contextlib import suppress
from git import Git, GitCommandError
from github.Repository import Repository
from msm import SkillRepo, SkillEntry
from os.path import join
from subprocess import call

from msk.exceptions import AlreadyUpdated, NotUploaded
from msk.global_context import GlobalContext
from msk.lazy import Lazy
from msk.util import skill_repo_name


class RepoData(GlobalContext):
    msminfo = Lazy(lambda s: s.msm.repo)  # type: SkillRepo
    git = Lazy(lambda s: Git(s.msminfo.path))  # type: Git
    hub = Lazy(lambda s: s.github.get_repo(skill_repo_name(s.msminfo.url)))  # type: Repository
    fork = Lazy(lambda s: s.github.get_user().create_fork(s.hub))  # type: Repository

    def push_to_fork(self, branch: str):
        remotes = self.git.remote().split('\n')
        command = 'set-url' if 'fork' in remotes else 'add'
        self.git.remote(command, 'fork', self.fork.html_url)

        # Use call to ensure the environment variable GIT_ASKPASS is used
        call(['git', 'push', '-u', 'fork', branch, '--force'], cwd=self.msminfo.path)

    def checkout_branch(self, branch):
        with suppress(GitCommandError):
            self.git.branch('-D', branch)
        try:
            self.git.checkout(b=branch)
        except GitCommandError:
            self.git.checkout(branch)


class SkillData(GlobalContext):
    def __init__(self, skill: SkillEntry):
        self.entry = skill

    name = property(lambda self: self.entry.name)
    repo = Lazy(lambda s: RepoData())  # type: RepoData
    repo_git = Lazy(lambda s: Git(join(s.repo.msminfo.path, s.submodule_name)))  # type: Git
    git = Lazy(lambda s: Git(s.entry.path))  # type: Git
    hub = Lazy(lambda s: s.github.get_repo(skill_repo_name(s.entry.url)))  # type: Repository

    @Lazy
    def submodule_name(self):
        name_to_path = {name: path for name, path, url, sha in self.repo.msminfo.get_skill_data()}
        if self.name not in name_to_path:
            raise NotUploaded('The skill {} has not yet been uploaded to the skill store'.format(
                self.name
            ))
        return name_to_path[self.name]

    def upgrade(self) -> str:
        skill_module = self.submodule_name
        self.repo.msminfo.update()
        self.repo_git.fetch()
        default_branch = self.repo_git.symbolic_ref('refs/remotes/origin/HEAD')
        self.repo_git.reset(default_branch, hard=True)

        upgrade_branch = 'upgrade/' + self.name
        self.repo.checkout_branch(upgrade_branch)

        if not self.repo.git.diff(skill_module) and self.repo.git.ls_files(skill_module):
            raise AlreadyUpdated(
                'The latest version of {} is already uploaded to the skill repo'.format(
                    self.name
                )
            )
        self.repo.git.add(skill_module)
        self.repo.git.commit(message='Upgrade ' + self.name)
        return upgrade_branch

    def add_to_repo(self) -> str:
        self.repo.msminfo.update()
        elements = [i.split() for i in self.git.ls_tree('HEAD').split('\n')]
        existing_mods = [folder for size, typ, sha, folder in elements]
        if self.name not in existing_mods:
            self.repo.git.submodule('add', self.entry.url, self.name)
        branch_name = 'add/' + self.name
        self.repo.checkout_branch(branch_name)
        self.repo.git.add(self.name)
        self.repo.git.commit(message='Add ' + self.name)
        return branch_name

    def init_existing(self):
        self.repo.git.submodule('update', '--init', self.submodule_name)