import paramiko
import os
 

def getConnect(host, port, username, password):
    """
    :param host: SFTP ip
    :param port: SFTP port
    :param username: SFTP userName
    :param password: SFTP password
    :return: sftp
    """
    print("SFTP connection...")
    result = [1, ""]
    sftp = None
    
    try:
        handle = paramiko.Transport((host, port))
        handle.connect(username=username, password=password)
        sftp = paramiko.SFTPClient.from_transport(handle)
        print("connection success")
    except Exception as e:
        sftp = None
        print("connection fail, reason:{0}".format(e))
 
    return sftp


def uploadFile(sftp, remoteDir, localPath):
    """
    :param sftp:
    :param remoteDir: server path
    :param localPath: client path
    :return:
    """
    print("start upload file by use SFTP...")
    result = [1, ""]
 
    try:
        try:
            sftp.chdir(remoteDir)
        except:
            sftp.mkdir(remoteDir)
            
        fileName = os.path.basename(localPath)
        remotePath = '{0}{1}'.format(remoteDir, fileName)
        sftp.put(localPath, remotePath)   
 
        result = [1, "upload " + fileName + " success"]
    except Exception as e:
        result = [-1, "upload fail, reason:{0}".format(e)]
 
    print(result[1])    
    return result

def downloadFile(sftp, remotePath, localAbsDir):  
    """ 
    :param handle: 
    :param remotePath: server path 
    :param localAbsDir: client path
    :return: 
    """  
    print("start download file by use SFTP...")  
    result = [1, ""]  
  
    try:   
        fileName = os.path.basename(remotePath)  
        localAbsPath = '{0}{1}'.format(localAbsDir, fileName)  
 
        lad = os.path.split(localAbsPath)[0]  
 
        if not os.path.exists(lad):  
            os.makedirs(lad)  
 
        sftp.get(remotePath, localAbsPath)  
 
        result = [1, "download " + localAbsPath + " success"]  
 
    except Exception as e:  
        result = [-1, "download fa,il, reason:{0}".format(e)]  
    
    print(result[1])
    return result

if __name__ == '__main__':
    sftp = getConnect(
    host="127.0.0.1",
    port=22,
    username='user',
    password='123456'
    )
    print(sftp)
    
    #uploadFile(sftp, path, path)
    #downloadFile(sftp,path, path)
        
    sftp.close()    
    
