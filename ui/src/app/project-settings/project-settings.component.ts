import { Component, OnInit, OnDestroy } from '@angular/core'
import { Router, ActivatedRoute } from '@angular/router'
import { UntypedFormGroup, UntypedFormControl } from '@angular/forms'
import { Title } from '@angular/platform-browser'

import { Subscription } from 'rxjs'

import { MessageService } from 'primeng/api'
import { ConfirmationService } from 'primeng/api'

import { AuthService } from '../auth.service'
import { BreadcrumbsService } from '../breadcrumbs.service'
import { ManagementService } from '../backend/api/management.service'

@Component({
    selector: 'app-project-settings',
    templateUrl: './project-settings.component.html',
    styleUrls: ['./project-settings.component.sass'],
})
export class ProjectSettingsComponent implements OnInit, OnDestroy {
    projectId = 0
    project: any = {
        name: '',
        branches: [],
        secrets: [],
        webhooks: {
            github_enabled: false,
            gitea_enabled: false,
            gitlab_enabled: false,
        },
    }

    newBranchDlgVisible = false
    branchDisplayName = ''
    branchRepoName = ''

    // secret form
    secretMode = 0
    secretForm = new UntypedFormGroup({
        id: new UntypedFormControl(''),
        name: new UntypedFormControl(''),
        kind: new UntypedFormControl(''),
        username: new UntypedFormControl(''),
        key: new UntypedFormControl(''),
        secret: new UntypedFormControl(''),
    })

    secretKinds = [
        {
            name: 'Simple Secret',
            value: 'simple',
        },
        {
            name: 'SSH Username & Key',
            value: 'ssh-key',
        },
    ]

    selectedSecret: any

    webhookServices = [
        {
            name: 'github',
            displayName: 'GitHub',
            logoUrl: '/assets/github-logo.svg',
        },
        {
            name: 'gitlab',
            displayName: 'GitLab',
            logoUrl: '/assets/gitlab-logo.svg',
        },
        {
            name: 'gitea',
            displayName: 'Gitea',
            logoUrl: '/assets/gitea-logo.svg',
        },
    ]

    private subs: Subscription = new Subscription()

    constructor(
        private route: ActivatedRoute,
        private router: Router,
        public auth: AuthService,
        private msgSrv: MessageService,
        private confirmationService: ConfirmationService,
        protected breadcrumbService: BreadcrumbsService,
        protected managementService: ManagementService,
        private titleService: Title
    ) {}

    ngOnInit() {
        this.subs.add(
            this.route.paramMap.subscribe((params) => {
                this.projectId = parseInt(params.get('id'), 10)
                this.refresh()
            })
        )
    }

    ngOnDestroy() {
        this.subs.unsubscribe()
    }

    refresh() {
        this.subs.add(
            this.managementService
                .getProject(this.projectId, true)
                .subscribe((project) => {
                    this.project = project
                    this.titleService.setTitle(
                        'Kraken - Project Settings ' + this.project.name
                    )

                    this.breadcrumbService.setCrumbs([
                        {
                            label: 'Projects',
                            project_id: this.projectId,
                            project_name: this.project.name,
                        },
                    ])

                    if (this.project.secrets.length === 0) {
                        this.secretMode = 1
                    } else {
                        this.selectSecret(this.project.secrets[0])
                    }

                    // calculate results per flow from runs
                    for (const branch of this.project.branches) {
                        for (const flow of branch.ci_flows) {
                            this.calculateFlowStats(flow)
                        }
                        for (const flow of branch.dev_flows) {
                            this.calculateFlowStats(flow)
                        }
                    }
                })
        )
    }

    calculateFlowStats(flow) {
        flow.tests_total = 0
        flow.tests_passed = 0
        flow.fix_cnt = 0
        flow.regr_cnt = 0
        flow.issues_new = 0
        for (const run of flow.runs) {
            flow.tests_total += run.tests_total
            flow.tests_passed += run.tests_passed
            flow.fix_cnt += run.fix_cnt
            flow.regr_cnt += run.regr_cnt
            flow.issues_new += run.issues_new
        }
        if (flow.tests_total > 0) {
            flow.tests_pass_ratio = (100 * flow.tests_passed) / flow.tests_total
            flow.tests_pass_ratio = flow.tests_pass_ratio.toFixed(1)
            if (flow.tests_total === flow.tests_passed) {
                flow.tests_color = 'var(--greenish1)'
            } else if (flow.tests_pass_ratio > 50) {
                flow.tests_color = 'var(--orangish1)'
            } else {
                flow.tests_color = 'var(--redish1)'
            }
        } else {
            flow.tests_color = 'white'
        }
    }

    newBranch() {
        this.newBranchDlgVisible = true
    }

    cancelNewBranch() {
        this.newBranchDlgVisible = false
    }

    newBranchKeyDown(event) {
        if (event.key === 'Enter') {
            this.addNewBranch()
        }
    }

    addNewBranch() {
        this.subs.add(
            this.managementService
                .createBranch(this.project.id, {
                    name: this.branchDisplayName,
                    branch_name: this.branchRepoName,
                })
                .subscribe(
                    (branch) => {
                        this.msgSrv.add({
                            severity: 'success',
                            summary: 'New branch succeeded',
                            detail: 'New branch operation succeeded.',
                        })
                        this.newBranchDlgVisible = false
                        this.router.navigate(['/branches/' + branch.id])
                    },
                    (err) => {
                        let msg = err.statusText
                        if (err.error && err.error.detail) {
                            msg = err.error.detail
                        }
                        this.msgSrv.add({
                            severity: 'error',
                            summary: 'New branch erred',
                            detail: 'New branch operation erred: ' + msg,
                            life: 10000,
                        })
                        this.newBranchDlgVisible = false
                    }
                )
        )
    }

    newSecret() {
        this.secretMode = 1
        this.secretForm.reset()
    }

    prepareSecret(secret) {
        const secretVal = {
            id: undefined,
            name: secret.name,
            kind: secret.kind,
            secret: undefined,
            username: undefined,
            key: undefined,
        }
        if (secret.kind === 'simple') {
            secretVal.secret = secret.secret
        } else if (secret.kind === 'ssh-key') {
            secretVal.username = secret.username
            secretVal.key = secret.key
        }
        return secretVal
    }

    secretAdd() {
        const secretVal = this.prepareSecret(this.secretForm.value)
        this.subs.add(
            this.managementService
                .createSecret(this.projectId, secretVal)
                .subscribe(
                    (data) => {
                        this.msgSrv.add({
                            severity: 'success',
                            summary: 'New secret succeeded',
                            detail: 'New secret operation succeeded.',
                        })
                        this.project.secrets.push(data)
                        this.selectSecret(data)
                    },
                    (err) => {
                        console.info(err)
                        let msg = err.statusText
                        if (err.error && err.error.detail) {
                            msg = err.error.detail
                        }
                        this.msgSrv.add({
                            severity: 'error',
                            summary: 'New secret erred',
                            detail: 'New secret operation erred: ' + msg,
                            life: 10000,
                        })
                    }
                )
        )
    }

    secretSave() {
        const secretVal = this.prepareSecret(this.secretForm.value)
        this.subs.add(
            this.managementService
                .updateSecret(this.secretForm.value.id, secretVal)
                .subscribe(
                    (secret) => {
                        for (const idx in this.project.secrets) {
                            if (this.project.secrets[idx].id === secret.id) {
                                this.project.secrets[idx] = secret
                                break
                            }
                        }
                        this.selectSecret(secret)
                        this.msgSrv.add({
                            severity: 'success',
                            summary: 'Secret update succeeded',
                            detail: 'Secret update operation succeeded.',
                        })
                    },
                    (err) => {
                        console.info(err)
                        let msg = err.statusText
                        if (err.error && err.error.detail) {
                            msg = err.error.detail
                        }
                        this.msgSrv.add({
                            severity: 'error',
                            summary: 'Secret update erred',
                            detail: 'Secret update operation erred: ' + msg,
                            life: 10000,
                        })
                    }
                )
        )
    }

    secretDelete() {
        const secretVal = this.secretForm.value
        this.confirmationService.confirm({
            message:
                'Do you really want to delete secret "' + secretVal.name + '"?',
            accept: () => {
                this.subs.add(
                    this.managementService.deleteSecret(secretVal.id).subscribe(
                        (secret) => {
                            this.refresh()
                            this.msgSrv.add({
                                severity: 'success',
                                summary: 'Secret deletion succeeded',
                                detail: 'Secret deletion operation succeeded.',
                            })
                        },
                        (err) => {
                            console.info(err)
                            let msg = err.statusText
                            if (err.error && err.error.detail) {
                                msg = err.error.detail
                            }
                            this.msgSrv.add({
                                severity: 'error',
                                summary: 'Secret deletion erred',
                                detail:
                                    'Secret deletion operation erred: ' + msg,
                                life: 10000,
                            })
                        }
                    )
                )
            },
        })
    }

    selectSecret(secret) {
        if (this.selectedSecret) {
            this.selectedSecret.selectedClass = ''
        }
        this.selectedSecret = secret
        this.selectedSecret.selectedClass = 'selectedClass'

        const secretVal = this.prepareSecret(secret)
        secretVal.id = secret.id
        if (secretVal.secret === undefined) {
            secretVal.secret = ''
        }
        if (secretVal.username === undefined) {
            secretVal.username = ''
        }
        if (secretVal.key === undefined) {
            secretVal.key = ''
        }
        this.secretForm.setValue(secretVal)

        this.secretMode = 2
    }

    getBaseUrl() {
        return window.location.origin
    }

    getOrGenerateSecret(service) {
        const secretKey = service + '_secret'
        if (!this.project.webhooks[secretKey]) {
            // generate random string, 20 chars
            const arr = new Uint8Array(20)
            window.crypto.getRandomValues(arr)
            const rndStr = Array.from(arr, (c) =>
                Math.round((36 * c) / 255).toString(36)
            ).join('')
            this.project.webhooks[secretKey] = rndStr
        }
        return this.project.webhooks[secretKey]
    }

    saveWebhooks() {
        const projectVal = { webhooks: this.project.webhooks }
        this.subs.add(
            this.managementService
                .updateProject(this.projectId, projectVal)
                .subscribe(
                    (project) => {
                        this.project = project
                        this.msgSrv.add({
                            severity: 'success',
                            summary: 'Project update succeeded',
                            detail: 'Project update operation succeeded.',
                        })
                    },
                    (err) => {
                        console.info(err)
                        let msg = err.statusText
                        if (err.error && err.error.detail) {
                            msg = err.error.detail
                        }
                        this.msgSrv.add({
                            severity: 'error',
                            summary: 'Project update erred',
                            detail: 'Project update operation erred: ' + msg,
                            life: 10000,
                        })
                    }
                )
        )
    }

    getFlows(branch) {
        return [
            { name: 'CI', flows: branch.ci_flows ? branch.ci_flows : [] },
            { name: 'Dev', flows: branch.dev_flows ? branch.dev_flows : [] },
        ]
    }

    deleteProject() {
        this.subs.add(
            this.managementService.deleteProject(this.projectId).subscribe(
                (data) => {
                    this.msgSrv.add({
                        severity: 'success',
                        summary: 'Project deletion succeeded',
                        detail: 'Project delete operation succeeded.',
                    })
                    this.router.navigate(['/'])
                },
                (err) => {
                    let msg = err.statusText
                    if (err.error && err.error.detail) {
                        msg = err.error.detail
                    }
                    this.msgSrv.add({
                        severity: 'error',
                        summary: 'Project deletion erred',
                        detail: 'Project delete operation erred: ' + msg,
                        life: 10000,
                    })
                }
            )
        )
    }
}
