from contextlib import suppress
from genericpath import samefile
from git import Git, GitCommandError
from github import Github
from github.AuthenticatedUser import AuthenticatedUser
from github.NamedUser import NamedUser
from github.Repository import Repository
from msm import MycroftSkillsManager
from os import chdir
from os.path import join
from subprocess import call

from msk import __version__
from msk.console_action import ConsoleAction
from msk.exceptions import NotUploaded, AlreadyUpdated, PRModified
from msk.util import ask_for_github_credentials


class UpgradeAction(ConsoleAction):
    def __init__(self, args):
        super().__init__(args)
        msm = MycroftSkillsManager()
        skill_matches = [
            skill
            for skill in msm.list()
            if skill.is_local and samefile(skill.path, args.skill_folder)
        ]
        if not skill_matches:
            raise NotUploaded('Skill at folder not uploaded to store: {}'.format(args.skill_folder))

        self.skill = skill_matches[0]
        self.repo = msm.repo

    def find_submodule_path(self):
        name_to_path = {name: path for name, path, url, sha in self.repo.get_skill_data()}
        if self.skill.name not in name_to_path:
            raise NotUploaded('The skill {} has not yet been uploaded to the skill store'.format(
                self.skill.name
            ))
        return name_to_path[self.skill.name]

    def load_git_repos(self):
        submodule_path = self.find_submodule_path()

        repo_git = Git(self.repo.path)
        repo_git.submodule('update', '--init', submodule_path)
        skill_git = Git(join(self.repo.path, submodule_path))
        return repo_git, skill_git

    def perform_local_upgrade(self, repo_git: Git, skill_git: Git) -> str:
        """Returns repo branch of upgrade"""
        repo_git.reset()
        skill_git.fetch()
        default_branch = skill_git.symbolic_ref('refs/remotes/origin/HEAD')
        skill_git.reset(default_branch, hard=True)
        upgrade_branch = 'upgrade/' + self.skill.name
        with suppress(GitCommandError):
            repo_git.branch('-D', upgrade_branch)
        repo_git.checkout(b=upgrade_branch)
        if not repo_git.diff(skill_git.working_dir):
            raise AlreadyUpdated(
                'The latest version of {} is already uploaded to the skill repo'.format(
                    self.skill.name)
            )
        repo_git.add(skill_git.working_dir)
        repo_git.commit(message='Upgrade ' + self.skill.name)
        return upgrade_branch

    def create_pr_message(self, skill_git: Git, skill_repo: Repository) -> tuple:
        """Reads git commits from skill repo to create a list of changes as the PR content"""
        title = 'Upgrade ' + self.skill.name
        body = 'This upgrades {} to include the following new commits:\n\n{}'.format(
            self.skill.name, '\n'.join(
                ' - [{}]({})'.format(
                    skill_git.show('-s', sha, format='%s'),
                    skill_repo.get_commit(sha).html_url
                )
                for sha in skill_git.rev_list(
                    '--ancestry-path', '{}..{}'.format(self.skill.sha, 'HEAD')
                ).split('\n')
            )
        )
        body += '\n\n<sub>Created with [mycroft-skills-kit]({}) v{}</sub>'.format(
            'https://github.com/mycroftai/mycroft-skills-kit', __version__
        )
        return title, body

    def setup_fork(self, repo_git: Git, user: AuthenticatedUser, skills_repo: Repository):
        """Create a fork if it doesn't exist and create a remote, 'fork', that points to it"""
        fork = user.create_fork(skills_repo)  # type: Repository
        remotes = repo_git.remote().split('\n')
        command = 'set-url' if 'fork' in remotes else 'add'
        repo_git.remote(command, 'fork', fork.html_url)

    def load_skill_repo(self, github: Github) -> Repository:
        """Gets the repository for the skill being upgraded"""
        skill_full_repo_name = '{}/{}'.format(
            self.skill.author, self.skill.extract_repo_name(self.skill.url)
        )
        return github.get_repo(skill_full_repo_name)

    def create_or_edit_pr(self, title: str, body: str, skills_repo: Repository,
                          user: NamedUser, upgrade_branch: str):
        base = skills_repo.default_branch
        head = '{}:{}'.format(user.login, upgrade_branch)
        pulls = list(skills_repo.get_pulls(base=base, head=head))
        if pulls:
            pull = pulls[0]
            if 'mycroft-skills-kit' in pull.body:
                pull.edit(title, body)
            else:
                raise PRModified('Not updating description since it was not autogenerated')
            return pull
        else:
            return skills_repo.create_pull(title, body, base=base, head=head)

    def push_to_fork(self, upgrade_branch):
        chdir(self.repo.path)

        # Use call to ensure the environment variable GIT_ASKPASS is used
        call(['git', 'push', '-u', 'fork', upgrade_branch, '--force'])

    def perform(self):
        self.repo.update()
        repo_git, skill_git = self.load_git_repos()
        upgrade_branch = self.perform_local_upgrade(repo_git, skill_git)

        github = ask_for_github_credentials()
        skills_repo = github.get_repo('MycroftAI/mycroft-skills')
        user = github.get_user()

        self.setup_fork(repo_git, user, skills_repo)
        self.push_to_fork(upgrade_branch)

        title, body = self.create_pr_message(skill_git, self.load_skill_repo(github))
        print()
        print('===', title, '===')
        print(body)
        print()
        pull = self.create_or_edit_pr(title, body, skills_repo, user, upgrade_branch)
        print('Created PR at:', pull.html_url)
