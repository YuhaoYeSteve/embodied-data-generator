"""Create timestamped contact sheets for inspection; never changes source media."""
import argparse
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw
from longform_pipeline import probe, atomic_json


def contact(video, output, times=None, columns=4, width=400):
    video,output=Path(video),Path(output)
    meta=probe(video)
    end=meta['video_duration']
    if times is None:
        times=[min(end-0.06,i*end/12) for i in range(12)]
        times[-1]=max(0,end-0.06)
    frames=output.parent/(output.stem+'_frames');frames.mkdir(parents=True,exist_ok=True)
    height=round(width*meta['height']/meta['width'])
    rows=(len(times)+columns-1)//columns
    board=Image.new('RGB',(columns*width,rows*(height+28)),(245,245,245))
    draw=ImageDraw.Draw(board)
    for i,sec in enumerate(times):
        dest=frames/('%02d.jpg'%i)
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-ss',str(sec),'-i',str(video),
                        '-frames:v','1','-vf','scale=%d:%d'%(width,height),str(dest)],check=True)
        with Image.open(dest) as frame: board.paste(frame,(i%columns*width,i//columns*(height+28)))
        draw.text((i%columns*width+8,i//columns*(height+28)+height+6),'t = %.3f s'%sec,fill='black')
    board.save(output)
    atomic_json(output.with_suffix('.json'),{'source':str(video),'media':meta,'sample_times':times,
               'limitation':'Sparse frames do not establish full motion or audio correctness.'})
    return output


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('video');p.add_argument('output');p.add_argument('--times')
    a=p.parse_args()
    print(contact(a.video,a.output,[float(t) for t in a.times.split(',')] if a.times else None))
