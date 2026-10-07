"""Read-only workspace summaries, with isolated ledgers and no cloud calls."""
import tempfile
import unittest
from pathlib import Path

from studio_backend import StudioManager

ROOT=Path(__file__).resolve().parents[1]


class WorkspaceSummaryTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.manager=StudioManager(ROOT,Path(self.folder.name),start_workers=False)
        self.addCleanup(self.manager.close)

    def spec(self, annotated=True):
        spec={'name':'零件归位','version':'1','global_prompt':'第一视角整理桌面','target_duration_seconds':60,
              'segments':[{'id':str(i),'duration':30,'prompt':'分类归位','required_actions':['拿起与放下'],
                           'action_labels':['grasp','place']} for i in range(2)]}
        if annotated:
            spec['dataset']={'source_scenario':'仓储工作台上的零件分类任务','task_type':'manipulation',
                'variant_id':'v1','variation':'标准摆放','task_label':'零件分类归位','objects':['零件','收纳盒'],
                'annotation_scope':'planned'}
        return spec

    def test_state_exposes_searchable_planned_labels_without_private_request_assets(self):
        result=self.manager.create_batch({'name':'目录测试','spec':self.spec(),'quantity':1})
        job=self.manager.state()['jobs'][0]
        self.assertEqual(job.get('task_label'),'零件分类归位')
        self.assertEqual(job.get('objects'),['零件','收纳盒'])
        self.assertEqual(job.get('action_labels'),['grasp','place'])
        self.assertEqual(job.get('source_scenario'),'仓储工作台上的零件分类任务')
        self.assertEqual(job.get('annotation_scope'),'planned')
        self.assertNotIn('assets',job)
        self.assertNotIn('request',job)
        metadata=self.manager.metadata(result['jobs'][0]['id'])
        self.assertEqual(metadata['spec']['dataset']['task_label'],job['task_label'])

    def test_legacy_spec_does_not_invent_a_task_label(self):
        self.manager.create_batch({'name':'旧脚本','spec':self.spec(False),'quantity':1})
        job=self.manager.state()['jobs'][0]
        self.assertEqual(job.get('task_label'),'')
        self.assertEqual(job.get('objects'),[])
        self.assertEqual(job.get('annotation_scope'),'planned')


if __name__=='__main__':unittest.main()
